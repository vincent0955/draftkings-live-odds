"""HTTP side: the page, a JSON snapshot, a Server-Sent Events stream, health.

Browsers get one SSE stream: a full board on connect, then only changes, plus
a status heartbeat every 2s. EventSource reconnects on its own, and each
reconnect starts with a fresh full board, so a browser can never drift.
"""
import asyncio
import json
import logging
import pathlib
import time
from contextlib import asynccontextmanager
from typing import Optional, Set

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .book import Book
from .config import Config
from .feed import LiveFeed, Status
from .latency import Latency
from .replay import ReplayFeed
from .simulate import SimulatedFeed

log = logging.getLogger(__name__)
STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
STATUS_EVERY = 2.0
CLIENT_QUEUE = 500
SOURCES = {"live": LiveFeed, "replay": ReplayFeed, "simulate": SimulatedFeed}


class Hub:
    """Fan-out to connected browsers. A browser that can't keep up gets its
    queue cleared and a full board instead, rather than slowing everyone."""

    def __init__(self) -> None:
        self.clients: Set[asyncio.Queue] = set()

    def publish(self, events: list) -> None:
        if not events:
            return
        if any(e.get("type") == "board" for e in events):
            item = ("board", None)
        else:
            item = ("changes", {"events": events, "server_emit": time.time()})
        for q in list(self.clients):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(("board", None))


def create_app(cfg: Optional[Config] = None) -> FastAPI:
    cfg = cfg or Config()
    book, hub, latency = Book(), Hub(), Latency()
    status = Status(source=cfg.source)
    source_cls = SOURCES.get(cfg.source)
    if source_cls is None:
        raise ValueError(f"SOURCE must be one of {sorted(SOURCES)}")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        feed = source_cls(cfg, book, hub.publish, status, latency)
        task = asyncio.create_task(feed.run())
        task.add_done_callback(lambda t: t.cancelled() or t.exception() and log.error("feed crashed: %r", t.exception()))
        yield
        task.cancel()

    app = FastAPI(title="DraftKings NFL odds", lifespan=lifespan)
    app.state.book, app.state.status, app.state.latency, app.state.hub = book, status, latency, hub

    def status_payload() -> dict:
        now = time.time()
        d = status.to_dict()
        d.update(
            server_time=now,
            confirmed_age=None if status.last_confirmed_at is None else round(now - status.last_confirmed_at, 1),
            latency=latency.summary(),
            counters=dict(book.stats),
            games=len(book.games),
        )
        return d

    def board_payload() -> dict:
        return {"games": book.board(), "status": status_payload()}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/odds")
    async def odds():
        return board_payload()

    @app.get("/api/time")
    async def server_time():
        return {"server_time": time.time()}

    @app.post("/api/latency")
    async def report_latency(request: Request):
        """Browsers report how long changes took to reach them (server emit ->
        browser receive, corrected for the browser's clock offset)."""
        try:
            body = await request.json()
            samples = [float(x) for x in body.get("samples", [])][:200]
        except Exception:
            return JSONResponse({"ok": False}, status_code=400)
        for ms in samples:
            if -1000 < ms < 60000:
                latency.add("server_to_browser", ms)
        return {"ok": True}

    @app.get("/health")
    async def health():
        s = status_payload()
        healthy = status.state in ("live", "replay", "simulate") or (
            status.state == "polling" and (s["confirmed_age"] or 1e9) < 30)
        return JSONResponse({"ok": healthy, **s}, status_code=200 if healthy else 503)

    @app.get("/api/stream")
    async def stream(request: Request):
        q: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
        hub.clients.add(q)

        def sse(event: str, data) -> str:
            return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"

        async def gen():
            try:
                yield "retry: 2000\n\n"
                yield sse("board", board_payload())
                last_status = time.time()
                while True:
                    try:
                        kind, data = await asyncio.wait_for(q.get(), timeout=STATUS_EVERY)
                        yield sse("board", board_payload()) if kind == "board" else sse(kind, data)
                    except asyncio.TimeoutError:
                        pass
                    if time.time() - last_status >= STATUS_EVERY:
                        yield sse("status", status_payload())
                        last_status = time.time()
                    if await request.is_disconnected():
                        break
            finally:
                hub.clients.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
