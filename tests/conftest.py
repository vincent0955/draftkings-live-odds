import base64
import json
import pathlib

import msgpack
import pytest

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures"


def load_json(name):
    return json.loads((FIX / name).read_text())


def recorded_frames(name):
    """Yield (direction, time, raw_bytes) for every captured socket frame."""
    data = load_json(name)
    for f in data["frames"]:
        yield f["dir"], f["t"], base64.b64decode(f["raw_b64"])


def subscriptions(name):
    """sub_id -> subscribe params, read from the frames the browser sent."""
    subs = {}
    for direction, _, raw in recorded_frames(name):
        if direction == "send":
            msg = msgpack.unpackb(raw, raw=False)
            if msg.get("method") == "subscribe":
                subs[msg["id"]] = msg["params"]
    return subs


@pytest.fixture
def nfl_day1():
    return load_json("nfl_game_lines_snapshot.json")


@pytest.fixture
def nfl_day2():
    return load_json("nfl_game_lines_snapshot_day2.json")


@pytest.fixture
def tennis_snapshot():
    return load_json("tennis_league_snapshot.json")
