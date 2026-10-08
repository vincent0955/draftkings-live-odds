"""SOURCE=replay: play a recorded live match through the real pipeline.

Uses the tennis capture (a live ATP match, ~5 minutes, odds changes and
suspensions) because no NFL game was live before the deadline. Every frame is
the original DraftKings bytes, decoded by the same code as live.
"""
import asyncio
import base64
import json
import pathlib
import time

import msgpack

from . import protocol, snapshot
from .book import Book
from .config import Config
from .feed import Publish, Status
from .latency import Latency

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"


def load_recording(fixtures: pathlib.Path = FIXTURES):
    snap = json.loads((fixtures / "tennis_league_snapshot.json").read_text())
    data = json.loads((fixtures / "ws_live_tennis_match.json").read_text())
    subs, frames = {}, []
    for f in data["frames"]:
        raw = base64.b64decode(f["raw_b64"])
        if f["dir"] == "send":
            m = msgpack.unpackb(raw, raw=False)
            if m.get("method") == "subscribe":
                subs[m["id"]] = m["params"]
            continue
        msg = protocol.decode(raw)
        params = subs.get(msg.sub_id)
        # The page held overlapping subscriptions; replay only the game-lines one.
        if msg.kind == "update" and params and "'6364'" in params["queryParams"]["query"]:
            frames.append((f["t"], raw, params["entity"]))
    return snap, frames


class ReplayFeed:
    def __init__(self, cfg: Config, book: Book, publish: Publish, status: Status, latency: Latency) -> None:
        self.cfg, self.book, self.publish, self.status = cfg, book, publish, status
        status.source = status.state = "replay"
        status.site = "recorded ATP match (Oct 7)"

    async def run(self) -> None:
        snap_data, frames = load_recording()
        ticker = asyncio.create_task(self._tick())
        try:
            while True:
                self.publish(self.book.load_snapshot(snapshot.normalize(snap_data), mode="replace"))
                self.status.note(f"replay: {len(frames)} recorded frames at {self.cfg.replay_speed}x")
                start_wall, start_rec = time.time(), frames[0][0]
                for t, raw, entity in frames:
                    delay = start_wall + (t - start_rec) / self.cfg.replay_speed - time.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    msg = protocol.decode(raw)
                    upd = protocol.parse_update(entity, msg, received=time.time())
                    # Shift DraftKings' timestamps to "now" so ordering checks still work.
                    shift = upd.received - t
                    upd.created = upd.created and upd.created + shift
                    upd.published = upd.published and upd.published + shift
                    upd.server_ts = upd.server_ts and upd.server_ts + shift
                    self.publish(self.book.apply(upd))
                await asyncio.sleep(3)
        finally:
            ticker.cancel()

    async def _tick(self) -> None:
        while True:
            self.status.last_confirmed_at = self.status.last_message_at = time.time()
            await asyncio.sleep(1)
