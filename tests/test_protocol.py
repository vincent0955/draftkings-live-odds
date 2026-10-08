import collections

import msgpack
import pytest

from app import protocol, wire
from app.config import Config
from tests.conftest import recorded_frames, subscriptions

TENNIS = "ws_live_tennis_match.json"


def test_every_recorded_live_frame_decodes():
    subs = subscriptions(TENNIS)
    counts, problems = collections.Counter(), []
    for direction, t, raw in recorded_frames(TENNIS):
        if direction != "receive":
            continue
        msg = protocol.decode(raw)
        if msg.kind != "update":
            continue
        entity = subs[msg.sub_id]["entity"]
        upd = protocol.parse_update(entity, msg, received=t)
        assert upd.published and upd.created and upd.server_ts
        problems += upd.problems
        for op in upd.ops:
            counts[(op.action, op.entity)] += 1
    assert problems == []
    assert counts == {
        ("change", "selection"): 337, ("change", "market"): 64, ("change", "event"): 15,
        ("insert", "selection"): 96, ("insert", "market"): 50,
        ("delete", "selection"): 94, ("delete", "market"): 49,
    }


def test_game_end_frames_decode():
    import json, base64
    from tests.conftest import FIX
    data = json.loads((FIX / "ws_game_end_deletes.json").read_text())
    deleted = collections.Counter()
    for sock in data["sockets"]:
        subs = {}
        for f in sock["frames"]:
            raw = base64.b64decode(f["raw_b64"])
            if f["dir"] == "send":
                m = msgpack.unpackb(raw, raw=False)
                if m.get("method") == "subscribe":
                    subs[m["id"]] = m["params"]["entity"]
                continue
            msg = protocol.decode(raw)
            if msg.kind == "update" and subs.get(msg.sub_id) in protocol.SLOTS:
                for op in protocol.parse_update(subs[msg.sub_id], msg, f["t"]).ops:
                    if op.action == "delete":
                        deleted[op.entity] += 1
    assert deleted["event"] >= 1 and deleted["market"] >= 1 and deleted["selection"] >= 2


def test_subscribe_matches_what_the_browser_sent():
    sent = [msgpack.unpackb(raw, raw=False) for d, _, raw in recorded_frames("ws_nfl_pregame_subscribe.json") if d == "send"]
    browser = next(m for m in sent if m.get("method") == "subscribe" and m["params"]["clientMetadata"]["feature"] == "league")
    ours = protocol.subscribe_message(Config(state="IL"), sub_id=browser["id"])
    assert ours["params"]["queryParams"]["query"] == browser["params"]["queryParams"]["query"]
    assert ours["params"]["queryParams"]["includeMarkets"] == browser["params"]["queryParams"]["includeMarkets"]
    assert ours["params"]["siteName"] == browser["params"]["siteName"] == "dkusil"
    assert msgpack.unpackb(protocol.encode(ours), raw=False) == ours


def test_garbage_is_rejected_cleanly():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b"\xc1\xc1 not msgpack")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(msgpack.packb({"unexpected": "shape"}))
    msg = protocol.decode(msgpack.packb(["sub", "update", "not a payload", None, [0, 0]]))
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_update("events", msg, 0)


def test_one_bad_item_does_not_sink_the_frame():
    good = wire.selection_change("0ML1_1", "A", -120)
    raw = wire.update_frame("sub", change_selections=[good, ["junk"], 42])
    upd = protocol.parse_update("events", protocol.decode(raw), received=0)
    assert [op.id for op in upd.ops] == ["0ML1_1"]
    assert len(upd.problems) == 2


def test_unknown_tag_is_flagged_for_resync():
    raw = wire.update_frame("sub", insert_markets=[[99, ["m1", "e1"]]])
    upd = protocol.parse_update("events", protocol.decode(raw), received=0)
    assert upd.ops[0].known is False
