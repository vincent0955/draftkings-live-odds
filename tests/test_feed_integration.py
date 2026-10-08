"""LiveFeed against a fake DraftKings (local socket + REST server).

Covers what the take-home asks for: keeps working when DraftKings drops the
connection, is unreachable, or sends something unexpected.
"""
import asyncio
import json
import time

import msgpack
import pytest
import websockets

from app import feed as feed_mod
from app import wire
from app.book import Book
from app.config import Config
from app.feed import LiveFeed, Status
from app.latency import Latency
from tests.conftest import load_json

TB_ML = "0ML84695570_3"  # TB moneyline, +350 in the day-1 snapshot
TB_KEY = ("1_84695570", "away")


class FakeDK:
    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self.rest_status, self.rest_delay, self.rest_hits = 200, 0.0, 0
        self.conns, self.on_subscribe = [], None

    async def start(self):
        self.ws_server = await websockets.serve(self._ws, "127.0.0.1", 0)
        self.http = await asyncio.start_server(self._http, "127.0.0.1", 0)
        wsp = self.ws_server.sockets[0].getsockname()[1]
        hp = self.http.sockets[0].getsockname()[1]
        return Config(state="IL", ws_url_override=f"ws://127.0.0.1:{wsp}/",
                      snapshot_url_override=f"http://127.0.0.1:{hp}/snap", reconcile_seconds=3600, poll_seconds=0.2)

    async def stop(self):
        self.ws_server.close()
        self.http.close()

    async def _http(self, reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.rest_hits += 1
            await asyncio.sleep(self.rest_delay)
            body = json.dumps(self.snapshot).encode() if self.rest_status == 200 else b"<html>Access Denied</html>"
            writer.write(f"HTTP/1.1 {self.rest_status} X\r\nContent-Type: application/json\r\n"
                         f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()

    async def _ws(self, ws):
        self.conns.append(ws)
        sub = msgpack.unpackb(await ws.recv(), raw=False)
        self.sub_id = sub["id"]
        await ws.send(wire.ack_frame(sub["id"]))
        if self.on_subscribe:
            await self.on_subscribe(ws, sub["id"])
        try:
            async for _ in ws:
                pass
        except websockets.ConnectionClosed:
            pass

    async def push(self, frame: bytes):
        await self.conns[-1].send(frame)


async def until(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def rig(monkeypatch):
    monkeypatch.setattr(feed_mod, "backoff_delay", lambda attempt: 0.3)
    dk = FakeDK(load_json("nfl_game_lines_snapshot.json"))
    cfg = await dk.start()
    book, status, latency, published = Book(), Status(), Latency(), []
    live = LiveFeed(cfg, book, published.extend, status, latency)
    yield dk, live, book, status, latency, published
    await dk.stop()


async def start(live):
    return asyncio.create_task(live.run())


async def test_goes_live_and_applies_updates_fast(rig):
    dk, live, book, status, latency, published = rig
    task = await start(live)
    await until(lambda: status.state == "live")
    assert len(book.games) == 15
    t0 = time.time()
    await dk.push(wire.update_frame(dk.sub_id, change_selections=[wire.selection_change(TB_ML, "TB", 375)]))
    await until(lambda: book.outcomes[TB_KEY].american == 375)
    assert time.time() - t0 < 0.5
    assert latency.summary()["dk_to_server"]["n"] == 1
    assert any(e.get("american") == 375 for e in published)
    task.cancel()


async def test_updates_during_snapshot_fetch_are_not_lost(rig):
    """Socket updates that arrive before REST returns are buffered and applied after."""
    dk, live, book, status, *_ = rig
    dk.rest_delay = 0.5

    async def early_update(ws, sub_id):
        await ws.send(wire.update_frame(sub_id, change_selections=[wire.selection_change(TB_ML, "TB", 390)]))
    dk.on_subscribe = early_update

    task = await start(live)
    await until(lambda: status.state == "live")
    assert book.outcomes[TB_KEY].american == 390  # not the snapshot's +350
    task.cancel()


async def test_reconnects_after_drop_and_polls_meanwhile(rig):
    dk, live, book, status, *_ = rig
    task = await start(live)
    await until(lambda: status.state == "live")
    hits_before = dk.rest_hits
    await dk.conns[-1].close()
    await until(lambda: status.reconnects == 1)
    await until(lambda: status.state == "live" and len(dk.conns) == 2)
    assert dk.rest_hits > hits_before  # re-snapshotted after the gap
    assert book.outcomes[TB_KEY].american == 350
    task.cancel()


async def test_garbage_from_draftkings_does_not_break_the_feed(rig):
    dk, live, book, status, *_ = rig
    task = await start(live)
    await until(lambda: status.state == "live")
    await dk.push(b"\xc1\xc1\xc1 definitely not msgpack")
    await dk.push(msgpack.packb([dk.sub_id, "update", {"surprise": True}, None, [0, 0]]))
    await dk.push(wire.update_frame(dk.sub_id, change_selections=[["junk"], wire.selection_change(TB_ML, "TB", 410)]))
    await until(lambda: book.outcomes[TB_KEY].american == 410)
    assert status.state == "live" and status.reconnects == 0
    assert book.stats["invalid_rows"] >= 3
    task.cancel()


async def test_draftkings_unreachable_then_recovers(rig):
    dk, live, book, status, *_ = rig
    dk.rest_status = 403  # e.g. bot protection kicks in
    task = await start(live)
    await until(lambda: status.reconnects >= 1)
    assert status.state in ("down", "connecting")
    assert "403" in (status.last_error or "")
    dk.rest_status = 200
    await until(lambda: status.state == "live", timeout=8)
    assert len(book.games) == 15
    task.cancel()


async def test_unknown_message_triggers_resync(rig):
    dk, live, book, status, *_ = rig
    live.cfg = Config(**{**live.cfg.__dict__, "reconcile_seconds": 3600})
    task = await start(live)
    await until(lambda: status.state == "live")
    hits = dk.rest_hits
    await dk.push(wire.update_frame(dk.sub_id, insert_markets=[[77, ["new-thing"]]]))  # unknown tag
    await until(lambda: dk.rest_hits > hits)
    await until(lambda: status.last_reconcile_at is not None)
    task.cancel()


def test_clock_offset_correction():
    lat = Latency()
    # DraftKings' clock runs 2s ahead of ours; subscribe round trip took 100ms.
    lat.set_dk_clock(sent_at=1000.0, ack_at=1000.1, dk_ts=1002.05)
    assert abs(lat.dk_clock_offset - 2.0) < 1e-9
    # An update published at DK 1012.0 (= our 1010.0) that we got at our 1010.08 took 80ms.
    lat.record_update(created=1011.98, published=1012.0, sent=1012.03, received=1010.08)
    s = lat.summary()
    assert s["dk_to_server"]["p50"] == 80.0 and s["wire"]["p50"] == 50.0 and s["dk_internal"]["p50"] == 20.0
    assert s["dk_clock"] == {"offset_ms": 2000.0, "uncertainty_ms": 50.0}


async def test_heartbeat_pings_and_confirms(rig, monkeypatch):
    dk, live, book, status, *_ = rig
    monkeypatch.setattr(feed_mod, "HEARTBEAT_SECONDS", 0.2)
    task = await start(live)
    await until(lambda: status.state == "live")
    first = status.last_confirmed_at
    await until(lambda: status.ping_ms is not None and status.last_confirmed_at > first, timeout=3)
    task.cancel()
