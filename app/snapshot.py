"""REST snapshot: fetch the full NFL game-lines board and normalize it."""
import json
import logging
import time
from typing import Tuple

import httpx

from . import parse
from .config import Config
from .model import Game, Market, SelectionRow, Snapshot

log = logging.getLogger(__name__)


def normalize(data: dict) -> Snapshot:
    """DraftKings' relational JSON (events / markets / selections) -> Snapshot.

    Every row is validated on its own. A bad row is skipped and recorded in
    snap.dropped; it never takes the rest of the board down with it.
    """
    snap = Snapshot()
    if not isinstance(data, dict):
        raise ValueError("snapshot is not a JSON object")

    for ev in data.get("events") or []:
        try:
            parts = {p.get("venueRole"): p.get("name") for p in ev.get("participants") or []}
            snap.games[str(ev["id"])] = Game(
                id=str(ev["id"]),
                name=ev.get("name") or "",
                away=parts.get("Away") or "",
                home=parts.get("Home") or "",
                start=parse.iso_ts(ev.get("startEventDate")),
                status=ev.get("status") or "NOT_STARTED",
            )
        except (KeyError, TypeError, AttributeError) as e:
            snap.dropped.append(f"event: {e!r}")

    for m in data.get("markets") or []:
        try:
            mtype = m.get("marketType") or {}
            kind = parse.market_kind(mtype.get("betOfferTypeId"), mtype.get("name") or m.get("name"))
            game_id = str(m["eventId"])
            if kind is None:
                snap.dropped.append(f"market {m.get('id')}: not a main market ({m.get('name')})")
                continue
            if game_id not in snap.games:
                snap.dropped.append(f"market {m.get('id')}: unknown event {game_id}")
                continue
            snap.markets[str(m["id"])] = Market(
                id=str(m["id"]), game_id=game_id, kind=kind, suspended=bool(m.get("isSuspended", False))
            )
        except (KeyError, TypeError, AttributeError) as e:
            snap.dropped.append(f"market: {e!r}")

    for s in data.get("selections") or []:
        try:
            mid = str(s["marketId"])
            market = snap.markets.get(mid)
            side = parse.side(s.get("outcomeType"))
            odds = s.get("displayOdds") or {}
            american = parse.american(odds.get("american"))
            if market is None:
                snap.dropped.append(f"selection {s.get('id')}: market not kept")
                continue
            if side is None or american is None:
                snap.dropped.append(f"selection {s.get('id')}: bad side/odds {s.get('outcomeType')!r} {odds.get('american')!r}")
                continue
            line = parse.points(s.get("points"))
            if market.kind != "moneyline" and line is None:
                snap.dropped.append(f"selection {s.get('id')}: {market.kind} without a line")
                continue
            snap.selections.append(
                SelectionRow(
                    id=str(s["id"]), market_id=mid, side=side, line=line, american=american,
                    decimal=parse.decimal_odds(s.get("trueOdds")) or parse.decimal_odds(odds.get("decimal")),
                )
            )
        except (KeyError, TypeError, AttributeError) as e:
            snap.dropped.append(f"selection: {e!r}")

    return snap


async def fetch(client: httpx.AsyncClient, cfg: Config) -> Tuple[Snapshot, float]:
    """Fetch and normalize. Returns (snapshot, fetched_at on our clock)."""
    started = time.time()
    resp = await client.get(cfg.snapshot_url, headers=cfg.http_headers, timeout=cfg.http_timeout)
    resp.raise_for_status()
    data = json.loads(resp.text)
    snap = normalize(data)
    if snap.dropped:
        log.info("snapshot: kept %d selections, dropped %d rows", len(snap.selections), len(snap.dropped))
    return snap, started
