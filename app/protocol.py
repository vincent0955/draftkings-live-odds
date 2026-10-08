"""DraftKings' live socket protocol (reverse-engineered from captured traffic).

Frames are MessagePack, JSON-RPC 2.0 style.

Client -> server:
    {"jsonrpc": "2.0", "method": "subscribe", "id": <uuid>, "params": {...}}

Server -> client (positional array):
    [sub_id, kind, payload, error, [server_send_ts, seq]]
    kind is "subscribed", "unsubscribe" or "update".

An update payload is [groups, None, {createdTime, receivedTime, publishedTime}].
groups is always 3 lists: [inserts, deletes, changes]. Each holds one list per
entity type, in a fixed order that depends on what was subscribed to:
    events subscription  -> [events, markets, selections, ...]
    markets subscription -> [markets, selections]
Deletes are bare ids. Inserts and changes are [type_tag, [fields...]] with
positional fields and no names. The positions below were inferred from
~700 live messages; every field we read is validated.
"""
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional

import msgpack

from . import parse
from .config import Config

SLOTS = {
    "events": ["event", "market", "selection"],
    "markets": ["market", "selection"],
}
ACTIONS = ["insert", "delete", "change"]

# type tags seen on the wire
TAG_EVENT_CHANGE = 22
TAG_MARKET_CHANGE = 23
TAG_SELECTION_CHANGE = 24
TAG_MARKET_FULL = 34
TAG_SELECTION_FULL = 35


def encode(obj: Any) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


def subscribe_message(cfg: Config, sub_id: Optional[str] = None) -> dict:
    """The exact subscription DraftKings' own NFL page sends."""
    return {
        "jsonrpc": "2.0",
        "method": "subscribe",
        "id": sub_id or str(uuid.uuid4()),
        "params": {
            "entity": "events",
            "queryParams": {
                "query": cfg.events_filter + " and tags/any(t: t eq 'OSB')",
                "initialData": False,
                "projection": "sportsbook",
                "locale": "en",
                "includeMarkets": cfg.markets_filter + " and tags/any(t: t eq 'OSB')",
            },
            "forwardedHeaders": {},
            "clientMetadata": {"feature": "league", "X-Client-Name": "web", "X-Client-Version": "2640.4.1.8"},
            "jwt": "default-token",
            "siteName": cfg.site_name,
        },
    }


@dataclass
class ServerMessage:
    sub_id: str
    kind: str
    payload: Any
    error: Any
    server_ts: Optional[float]


class ProtocolError(ValueError):
    pass


def decode(raw: bytes) -> ServerMessage:
    try:
        obj = msgpack.unpackb(raw, raw=False, timestamp=1, strict_map_key=False)
    except Exception as e:  # msgpack raises several types
        raise ProtocolError(f"not msgpack: {e!r}") from e
    if not isinstance(obj, list) or len(obj) < 2 or not isinstance(obj[0], str):
        raise ProtocolError(f"unexpected frame shape: {str(obj)[:120]}")
    server_ts = None
    if len(obj) > 4 and isinstance(obj[4], list) and obj[4] and isinstance(obj[4][0], (int, float)):
        server_ts = float(obj[4][0])
    return ServerMessage(
        sub_id=obj[0], kind=str(obj[1]),
        payload=obj[2] if len(obj) > 2 else None,
        error=obj[3] if len(obj) > 3 else None,
        server_ts=server_ts,
    )


@dataclass
class Op:
    action: str  # insert | delete | change
    entity: str  # event | market | selection
    id: str
    fields: dict = field(default_factory=dict)
    known: bool = True  # False when we couldn't map the tag; caller should resync


@dataclass
class Update:
    ops: List[Op]
    created: Optional[float]
    published: Optional[float]
    server_ts: Optional[float]
    received: float
    problems: List[str] = field(default_factory=list)


def _get(f: list, i: int):
    return f[i] if i < len(f) else None


def _parse_item(action: str, entity: str, item) -> Op:
    if action == "delete":
        if not isinstance(item, str):
            raise ProtocolError(f"delete id is not a string: {item!r}")
        return Op(action, entity, item)

    if not (isinstance(item, list) and len(item) == 2 and isinstance(item[1], list)):
        raise ProtocolError(f"bad {action} item: {str(item)[:120]}")
    tag, f = item
    if not f or not isinstance(f[0], str):
        raise ProtocolError(f"{entity} {action} without id")
    oid = f[0]

    if entity == "selection" and tag == TAG_SELECTION_CHANGE:
        odds = _get(f, 2) or []
        return Op(action, entity, oid, {
            "label": _get(f, 1),
            "american": parse.american(odds[0] if odds else None),
            "decimal": parse.decimal_odds(_get(f, 3)),
            "line": parse.points(_get(f, 4)),
        })
    if entity == "selection" and tag == TAG_SELECTION_FULL:
        odds = _get(f, 3) or []
        return Op(action, entity, oid, {
            "market_id": _get(f, 1),
            "label": _get(f, 2),
            "american": parse.american(odds[0] if odds else None),
            "decimal": parse.decimal_odds(_get(f, 4)),
            "line": parse.points(_get(f, 5)),
            "side": parse.side(_get(f, 6)),
        })
    if entity == "market" and tag == TAG_MARKET_CHANGE:
        susp = _get(f, 2)
        return Op(action, entity, oid, {"name": _get(f, 1), "suspended": susp if isinstance(susp, bool) else None})
    if entity == "market" and tag == TAG_MARKET_FULL:
        mtype = _get(f, 7) or [None, None, None]
        susp = _get(f, 5)
        return Op(action, entity, oid, {
            "game_id": _get(f, 1),
            "name": _get(f, 4),
            "suspended": susp if isinstance(susp, bool) else False,
            "kind": parse.market_kind(_get(mtype, 1), _get(mtype, 2) or _get(f, 4)),
        })
    if entity == "event" and tag == TAG_EVENT_CHANGE:
        parts = {}
        for p in _get(f, 2) or []:
            if isinstance(p, list) and len(p) > 2:
                parts[p[2]] = p[1]
        return Op(action, entity, oid, {
            "start": _get(f, 1) if isinstance(_get(f, 1), (int, float)) else None,
            "away": parts.get("Away"),
            "home": parts.get("Home"),
            "status": _get(f, 3) if isinstance(_get(f, 3), str) else None,
        })
    # Anything else (e.g. a brand-new event): we know *that* something
    # changed but not how to read it. Flag it so the feed re-fetches the snapshot.
    return Op(action, entity, oid, {"tag": tag}, known=False)


def parse_update(entity: str, msg: ServerMessage, received: float) -> Update:
    """Turn one 'update' frame into a flat list of Ops. Bad items are skipped
    and listed in update.problems; one bad item never sinks the frame."""
    payload = msg.payload
    if not (isinstance(payload, list) and payload and isinstance(payload[0], list) and len(payload[0]) == 3):
        raise ProtocolError(f"unexpected update payload: {str(payload)[:160]}")
    meta = payload[2] if len(payload) > 2 and isinstance(payload[2], dict) else {}
    slots = SLOTS.get(entity)
    if slots is None:
        raise ProtocolError(f"no slot layout for entity {entity!r}")

    upd = Update(
        ops=[], created=parse.iso_ts(meta.get("createdTime")),
        published=parse.iso_ts(meta.get("publishedTime")),
        server_ts=msg.server_ts, received=received,
    )
    for action, group in zip(ACTIONS, payload[0]):
        if not isinstance(group, list):
            upd.problems.append(f"{action} group is not a list")
            continue
        for slot_name, items in zip(slots, group):
            for item in items or []:
                try:
                    upd.ops.append(_parse_item(action, slot_name, item))
                except ProtocolError as e:
                    upd.problems.append(str(e))
        # Slots beyond the ones we map (seen empty so far) are ignored but noted.
        for extra in group[len(slots):]:
            if extra:
                upd.problems.append(f"unmapped {action} slot with {len(extra)} items")
    return upd
