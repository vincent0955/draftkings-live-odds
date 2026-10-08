"""Build frames in DraftKings' wire format.

Used by tests and SOURCE=simulate, so synthetic NFL moves go through exactly
the same decoder as real ones. Field positions mirror protocol.py.
"""
import time
from datetime import datetime, timezone
from typing import List, Optional

import msgpack

from .protocol import TAG_MARKET_CHANGE, TAG_SELECTION_CHANGE, TAG_SELECTION_FULL


def _american_str(n: int) -> str:
    return f"+{n}" if n > 0 else f"−{abs(n)}"


def _decimal(n: int) -> float:
    return round(1 + (n / 100 if n > 0 else 100 / abs(n)), 8)


def _odds(n: int) -> list:
    d = _decimal(n)
    return [_american_str(n), f"{d:.2f}", "", "", "", f"{d:.2f}x"]


def selection_change(sel_id: str, label: str, american: int, line: Optional[float] = None) -> list:
    return [TAG_SELECTION_CHANGE, [sel_id, label, _odds(american), _decimal(american), line, 0, ["OSB"], None]]


def selection_full(sel_id: str, market_id: str, label: str, american: int,
                   line: Optional[float], outcome_type: str) -> list:
    return [TAG_SELECTION_FULL, [sel_id, market_id, label, _odds(american), _decimal(american), line,
                                 outcome_type, [], 0, ["MainPointLine", "OSB"], None, None, {}]]


def market_change(market_id: str, name: str, suspended: bool) -> list:
    return [TAG_MARKET_CHANGE, [market_id, name, suspended, ["OSB"], 0, None, False, None]]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def update_frame(sub_id: str, *, entity: str = "events",
                 insert_markets: List = (), insert_selections: List = (),
                 delete_events: List = (), delete_markets: List = (), delete_selections: List = (),
                 change_markets: List = (), change_selections: List = (),
                 published: Optional[float] = None, sent: Optional[float] = None) -> bytes:
    published = published or time.time()
    sent = sent or published + 0.03
    if entity == "events":
        groups = [
            [[], list(insert_markets), list(insert_selections), [], []],
            [list(delete_events), list(delete_markets), list(delete_selections), [], []],
            [[], list(change_markets), list(change_selections), [], []],
        ]
    else:
        groups = [
            [list(insert_markets), list(insert_selections)],
            [list(delete_markets), list(delete_selections)],
            [list(change_markets), list(change_selections)],
        ]
    meta = {"createdTime": _iso(published - 0.02), "receivedTime": _iso(published - 0.01), "publishedTime": _iso(published)}
    frame = [sub_id, "update", [groups, None, meta], None,
             [msgpack.Timestamp.from_unix(sent), 0]]
    return msgpack.packb(frame, use_bin_type=True)


def ack_frame(sub_id: str, kind: str = "subscribed") -> bytes:
    return msgpack.packb([sub_id, kind, None, None, [msgpack.Timestamp.from_unix(time.time()), 0]], use_bin_type=True)
