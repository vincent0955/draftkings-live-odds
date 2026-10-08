"""SOURCE=simulate: fake NFL line movement in DraftKings' exact wire format.

Starts from a real NFL snapshot and invents price moves, suspensions and line
moves (delete old selection + insert a new id, as DraftKings does). Frames are
encoded with app.wire and decoded by the real protocol code, so this exercises
the full path. Clearly labeled SIMULATED in the UI.
"""
import asyncio
import json
import random
import time

from . import protocol, snapshot, wire
from .book import Book
from .config import Config
from .feed import Publish, Status
from .latency import Latency
from .replay import FIXTURES

VIG = 0.045


def to_prob(american: int) -> float:
    return 100 / (american + 100) if american > 0 else -american / (-american + 100)


def to_american(p: float) -> int:
    p = min(max(p, 0.02), 0.98)
    v = -100 * p / (1 - p) if p >= 0.5 else 100 * (1 - p) / p
    v = int(round(v))
    return v if abs(v) >= 100 else (100 if v > 0 else -100)


def priced_pair(p_first: float):
    """Fair probability for the first side -> two american prices with vig."""
    return to_american(p_first + VIG / 2), to_american(1 - p_first + VIG / 2)


def selection_id(kind: str, market_id: str, side: str, line: float) -> str:
    num = market_id.split("_", 1)[1]
    suffix = "1" if side in ("home", "over") else "3"
    if kind == "spread":
        return f"0HC{num}{'N' if line < 0 else 'P'}{int(round(abs(line) * 100)):03d}_{suffix}"
    return f"0OU{num}{'O' if side == 'over' else 'U'}{int(round(line * 100))}_{suffix}"


class SimulatedFeed:
    PAIRS = {"moneyline": ("away", "home"), "spread": ("away", "home"), "total": ("over", "under")}

    def __init__(self, cfg: Config, book: Book, publish: Publish, status: Status, latency: Latency) -> None:
        self.cfg, self.book, self.publish, self.status = cfg, book, publish, status
        status.source = status.state = "simulate"
        status.site = "simulated moves on a real NFL snapshot"

    async def run(self) -> None:
        data = json.loads((FIXTURES / "nfl_game_lines_snapshot_day2.json").read_text())
        self.publish(self.book.load_snapshot(snapshot.normalize(data), mode="replace"))
        self.status.note("simulate: random moves every ~1.5s")
        while True:
            await asyncio.sleep(random.uniform(0.5, 2.5) / self.cfg.replay_speed)
            self.status.last_confirmed_at = self.status.last_message_at = time.time()
            markets = [m for m in self.book.markets.values() if not m.suspended]
            if not markets:
                continue
            m = random.choice(markets)
            r = random.random()
            if r < 0.65:
                self._send(change_selections=self._reprice(m, random.uniform(-0.04, 0.04)))
            elif r < 0.80:
                self._send(change_markets=[wire.market_change(m.id, m.kind, True)])
                asyncio.get_running_loop().call_later(random.uniform(1, 4), self._reopen, m.id)
            elif m.kind != "moneyline":
                self._move_line(m)

    def _send(self, **parts) -> None:
        raw = wire.update_frame("sim", entity="events", published=time.time(), **parts)
        upd = protocol.parse_update("events", protocol.decode(raw), received=time.time())
        self.publish(self.book.apply(upd))

    def _reopen(self, market_id: str) -> None:
        m = self.book.markets.get(market_id)
        if m is not None:
            self._send(change_markets=[wire.market_change(m.id, m.kind, False)],
                       change_selections=self._reprice(m, random.uniform(-0.06, 0.06)))

    def _pair(self, m):
        a, b = self.PAIRS[m.kind]
        return self.book.outcomes.get((m.id, a)), self.book.outcomes.get((m.id, b))

    def _reprice(self, m, nudge: float):
        first, second = self._pair(m)
        if not first or not second or first.american is None:
            return []
        p = to_prob(first.american) - VIG / 2 + nudge
        a1, a2 = priced_pair(min(max(p, 0.05), 0.95))
        return [wire.selection_change(first.selection_id, "", a1, first.line),
                wire.selection_change(second.selection_id, "", a2, second.line)]

    def _move_line(self, m) -> None:
        first, second = self._pair(m)
        if not first or not second or first.line is None:
            return
        step = random.choice((-0.5, 0.5))
        if m.kind == "spread":
            new_first = first.line + step
            if new_first == 0:
                return
            lines = (new_first, -new_first)
        else:
            lines = (first.line + step, first.line + step)
        a1, a2 = priced_pair(0.5 + random.uniform(-0.02, 0.02))
        sides = self.PAIRS[m.kind]
        game = self.book.games.get(m.game_id)
        labels = {"away": game.away if game else "Away", "home": game.home if game else "Home",
                  "over": "Over", "under": "Under"}
        inserts = [
            wire.selection_full(selection_id(m.kind, m.id, side, line), m.id, labels[side], price, line, side.title())
            for side, line, price in zip(sides, lines, (a1, a2))
        ]
        self._send(delete_selections=[first.selection_id, second.selection_id], insert_selections=inserts)
