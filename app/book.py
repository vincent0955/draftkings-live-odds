"""In-memory odds book: the current state of every NFL main line.

Two inputs:
  load_snapshot()  full board from REST (startup, reconnect, reconciliation)
  apply()          one socket update (only what changed)

Both return a list of change events for the browser.
"""
import logging
import time
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

from .model import Game, Market, Outcome, Snapshot
from .protocol import Op, Update

log = logging.getLogger(__name__)

Key = Tuple[str, str]  # (market_id, side)
BOARD = {"type": "board"}  # structural change: browser should re-render everything


class Book:
    def __init__(self) -> None:
        self.games: Dict[str, Game] = {}
        self.markets: Dict[str, Market] = {}
        self.outcomes: Dict[Key, Outcome] = {}
        self.sel_index: Dict[str, Key] = {}  # selection id -> (market_id, side)
        self.needs_resync = False
        self._suspects: Dict[Key, tuple] = {}  # REST disagreements awaiting a second look
        self.stats = {
            "updates": 0, "ops": 0, "changes": 0, "dropped_out_of_order": 0,
            "ignored_unknown_ids": 0, "invalid_rows": 0, "unknown_ops": 0, "drift_fixed": 0, "snapshot_changes": 0,
        }

    # ------------------------------------------------------------ snapshot
    def load_snapshot(self, snap: Snapshot, mode: str = "replace", now: Optional[float] = None) -> List[dict]:
        """Bring the board in line with a REST snapshot.

        mode="replace": startup, reconnect, or polling while the socket is down.
          REST wins. On (re)connect the feed subscribes *before* fetching REST
          and applies the buffered socket updates *after* this call, so
          anything newer than the snapshot still ends up on top.

        mode="reconcile": the once-a-minute safety net while the socket is up.
          In the captures, REST lagged the socket by several seconds at least
          once, so a single REST read can be *older* than what we hold. A value
          is only corrected when REST disagrees with us on two reconciles in a
          row and the socket hasn't touched it in between. Those corrections
          are "drift": socket updates we must have missed. Should stay at 0.

        We never compare DraftKings' timestamps with our own clock: the two
        were 0.4 to 1.7 seconds apart in the captures. Values loaded from REST
        get as_of=0, so the next socket update always applies on top.

        New and removed games/markets are applied in both modes.
        """
        now = now or time.time()
        reconcile = mode == "reconcile"
        events: List[dict] = []
        structural = set(snap.games) != set(self.games) or set(snap.markets) != set(self.markets)

        old_outcomes, old_markets = self.outcomes, self.markets
        self.games = {gid: self.games.get(gid, g) if reconcile else g for gid, g in snap.games.items()}
        self.markets = {}
        for mid, m in snap.markets.items():
            prev = old_markets.get(mid)
            if prev is not None and reconcile:
                self.markets[mid] = prev  # the socket owns suspension state while it's up
                continue
            self.markets[mid] = Market(m.id, m.game_id, m.kind, m.suspended, 0.0)
            if prev is not None and prev.suspended != m.suspended:
                events.append(self._market_event(self.markets[mid]))

        suspects: Dict[Key, tuple] = {}
        self.outcomes, self.sel_index = {}, {}
        for row in snap.selections:
            key = (row.market_id, row.side)
            old = old_outcomes.get(key)
            rest_value = (row.id, row.line, row.american)
            if old is not None:
                held = (old.selection_id, old.line, old.american) if old.available else None
                keep_old = held == rest_value
                if reconcile and not keep_old:
                    confirmed = self._suspects.get(key) == (rest_value, old.as_of)
                    keep_old = not confirmed
                    if not confirmed:
                        suspects[key] = (rest_value, old.as_of)
                if keep_old:
                    self.outcomes[key] = old
                    if old.available:
                        self.sel_index[old.selection_id] = key
                    continue
            out = Outcome(row.market_id, row.side, row.id, row.line, row.american, row.decimal, as_of=0.0)
            if old is not None:
                out.prev_line, out.prev_american, out.changed_at = old.line, old.american, now
                self.stats["drift_fixed" if reconcile else "snapshot_changes"] += 1
                events.append(self._outcome_event(out, None))
            self.outcomes[key] = out
            self.sel_index[row.id] = key

        self._suspects = suspects
        self.needs_resync = False
        if structural:
            return [BOARD]
        return events

    # -------------------------------------------------------------- socket
    def apply(self, upd: Update, now: Optional[float] = None) -> List[dict]:
        now = now or time.time()
        self.stats["updates"] += 1
        self.stats["invalid_rows"] += len(upd.problems)
        published = upd.published or upd.server_ts or upd.received
        events: List[dict] = []
        for op in upd.ops:
            self.stats["ops"] += 1
            if not op.known:
                self.stats["unknown_ops"] += 1
                self.needs_resync = True
                continue
            handler = getattr(self, f"_{op.action}_{op.entity}", None)
            if handler is None:
                continue
            ev = handler(op, published, now, upd)
            if ev:
                events.append(ev)
        if any(e is BOARD for e in events):
            return [BOARD]
        self.stats["changes"] += len(events)
        return events

    # selections ---------------------------------------------------------
    def _change_selection(self, op: Op, published: float, now: float, upd: Update):
        key = self.sel_index.get(op.id)
        if key is None:
            self.stats["ignored_unknown_ids"] += 1
            return None
        out = self.outcomes[key]
        if published < out.as_of:
            self.stats["dropped_out_of_order"] += 1
            return None
        american = op.fields.get("american")
        if american is None:
            self.stats["invalid_rows"] += 1
            return None
        line = op.fields.get("line") if op.fields.get("line") is not None else out.line
        out.as_of = published
        if (american, line) == (out.american, out.line):
            return None
        out.prev_american, out.prev_line = out.american, out.line
        out.american, out.line = american, line
        out.decimal = op.fields.get("decimal") or out.decimal
        out.changed_at = now
        return self._outcome_event(out, upd)

    def _insert_selection(self, op: Op, published: float, now: float, upd: Update):
        """A new selection. For an existing market this is how a line move
        shows up: the old selection is deleted and a new id is inserted."""
        f = op.fields
        mid, side, american = f.get("market_id"), f.get("side"), f.get("american")
        market = self.markets.get(str(mid)) if mid is not None else None
        if market is None:
            self.stats["ignored_unknown_ids"] += 1
            return None
        if side is None or american is None or (market.kind != "moneyline" and f.get("line") is None):
            self.stats["invalid_rows"] += 1
            return None
        key = (market.id, side)
        out = self.outcomes.get(key)
        if out is not None and published < out.as_of:
            self.stats["dropped_out_of_order"] += 1
            return None
        if out is None:
            out = Outcome(market.id, side, op.id, f.get("line"), american, f.get("decimal"), as_of=published)
            self.outcomes[key] = out
        else:
            if out.selection_id != op.id:
                self.sel_index.pop(out.selection_id, None)
            if (out.line, out.american) != (f.get("line"), american):
                out.prev_line, out.prev_american = out.line, out.american
            out.selection_id, out.line, out.american = op.id, f.get("line"), american
            out.decimal, out.available, out.as_of = f.get("decimal"), True, published
        out.changed_at = now
        self.sel_index[op.id] = key
        return self._outcome_event(out, upd)

    def _delete_selection(self, op: Op, published: float, now: float, upd: Update):
        key = self.sel_index.pop(op.id, None)
        if key is None:
            return None
        out = self.outcomes[key]
        if published < out.as_of:
            self.stats["dropped_out_of_order"] += 1
            self.sel_index[op.id] = key
            return None
        # Keep the row (and its last price) but mark it off the board until
        # the replacement selection arrives.
        out.available, out.as_of, out.changed_at = False, published, now
        return self._outcome_event(out, upd)

    # markets ------------------------------------------------------------
    def _change_market(self, op: Op, published: float, now: float, upd: Update):
        m = self.markets.get(op.id)
        susp = op.fields.get("suspended")
        if m is None or susp is None:
            return None
        if published < m.as_of:
            self.stats["dropped_out_of_order"] += 1
            return None
        m.as_of = published
        if m.suspended == susp:
            return None
        m.suspended = susp
        return self._market_event(m)

    def _insert_market(self, op: Op, published: float, now: float, upd: Update):
        f = op.fields
        gid = str(f.get("game_id"))
        if f.get("kind") is None or gid not in self.games or op.id in self.markets:
            return None
        self.markets[op.id] = Market(op.id, gid, f["kind"], bool(f.get("suspended")), published)
        return BOARD

    def _delete_market(self, op: Op, published: float, now: float, upd: Update):
        if self.markets.pop(op.id, None) is None:
            return None
        for key in [k for k in self.outcomes if k[0] == op.id]:
            self.sel_index.pop(self.outcomes.pop(key).selection_id, None)
        return BOARD

    # events -------------------------------------------------------------
    def _change_event(self, op: Op, published: float, now: float, upd: Update):
        g = self.games.get(op.id)
        if g is None:
            self.needs_resync = True  # a game we don't know about yet
            return None
        status = op.fields.get("status")
        if status and status != g.status:
            g.status = status
            return {"type": "game", "game_id": g.id, "status": g.status}
        return None

    def _delete_event(self, op: Op, published: float, now: float, upd: Update):
        if self.games.pop(op.id, None) is None:
            return None
        for mid in [mid for mid, m in self.markets.items() if m.game_id == op.id]:
            self._delete_market(Op("delete", "market", mid), published, now, upd)
        return BOARD

    def _insert_event(self, op: Op, published: float, now: float, upd: Update):
        self.needs_resync = True
        return None

    # ------------------------------------------------------------ output
    def _outcome_event(self, out: Outcome, upd: Optional[Update]) -> dict:
        m = self.markets.get(out.market_id)
        ev = {
            "type": "outcome",
            "game_id": m.game_id if m else None,
            "market": m.kind if m else None,
            **self._outcome_json(out),
        }
        if upd is not None:
            ev["timing"] = {
                "dk_created": upd.created, "dk_published": upd.published,
                "dk_sent": upd.server_ts, "server_received": upd.received,
            }
        return ev

    def _market_event(self, m: Market) -> dict:
        return {"type": "market", "game_id": m.game_id, "market": m.kind, "market_id": m.id, "suspended": m.suspended}

    @staticmethod
    def _outcome_json(out: Outcome) -> dict:
        d = asdict(out)
        d.pop("as_of", None)
        return d

    def board(self) -> List[dict]:
        """Full board in display order: games by kickoff, each with ML / spread / total."""
        by_game: Dict[str, dict] = {}
        for g in sorted(self.games.values(), key=lambda g: (g.start or 0, g.name)):
            by_game[g.id] = {**asdict(g), "markets": {}}
        for m in self.markets.values():
            game = by_game.get(m.game_id)
            if game is None:
                continue
            game["markets"][m.kind] = {"id": m.id, "suspended": m.suspended, "outcomes": {}}
        for out in self.outcomes.values():
            m = self.markets.get(out.market_id)
            if m is None or m.game_id not in by_game:
                continue
            by_game[m.game_id]["markets"][m.kind]["outcomes"][out.side] = self._outcome_json(out)
        return list(by_game.values())
