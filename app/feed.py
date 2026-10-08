"""Live ingest: one socket to DraftKings, REST for the base state and recovery.

Connection lifecycle:
  1. open the socket and subscribe (updates start arriving; we buffer them)
  2. fetch the REST snapshot and load it
  3. apply the buffered updates on top, then apply live as they arrive
  4. every 5s, ping the socket; no pong within 5s means the connection is dead
  5. on any failure: reconnect with exponential backoff + jitter, and poll
     the REST snapshot while waiting so the page doesn't freeze
  6. every 60s (and whenever we see something we can't decode): re-fetch REST
     and reconcile, to catch anything the socket missed

"Stale" is about connection health, not price movement. A line can sit still
for an hour; that's fine as long as we can prove the socket is alive.
"""
import asyncio
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

import httpx
import websockets

from . import protocol, snapshot
from .book import Book
from .config import ORIGIN, UA, Config
from .latency import Latency

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 5.0


@dataclass
class Status:
    source: str = "live"
    state: str = "starting"  # starting | connecting | live | polling | down | replay | simulate
    site: str = ""
    connected_since: Optional[float] = None
    last_message_at: Optional[float] = None
    last_confirmed_at: Optional[float] = None  # last moment we *know* our data was current
    last_snapshot_at: Optional[float] = None
    last_reconcile_at: Optional[float] = None
    reconnects: int = 0
    ping_ms: Optional[float] = None
    last_error: Optional[str] = None
    # Where the starting board comes from. "server": we fetch DraftKings' REST
    # snapshot. "browser": REST is blocked for this server (Akamai returns 403
    # to AWS IPs), so each browser fetches it and we stream socket changes on top.
    snapshot: str = "server"
    snapshot_error: Optional[str] = None
    log: List[str] = field(default_factory=list)  # recent connection events, newest last

    def note(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S", time.gmtime())
        self.log = (self.log + [f"{stamp}Z {text}"])[-20:]
        log.info(text)

    def to_dict(self) -> dict:
        return asdict(self)


Publish = Callable[[list], None]


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """1s, 2s, 4s ... capped at 30s, randomized to 50-100% so many clients
    (or many restarts) don't reconnect in lockstep."""
    return random.uniform(0.5, 1.0) * min(cap, base * (2 ** attempt))


class LiveFeed:
    def __init__(self, cfg: Config, book: Book, publish: Publish, status: Status, latency: Latency) -> None:
        self.cfg, self.book, self.publish, self.status, self.latency = cfg, book, publish, status, latency
        self.status.site = cfg.site_code
        self.client: Optional[httpx.AsyncClient] = None
        self._resync = asyncio.Event()

    async def run(self) -> None:
        # REST may go through a proxy (DK_HTTP_PROXY); the socket below never does.
        async with httpx.AsyncClient(proxy=self.cfg.http_proxy or None) as client:
            self.client = client
            reconciler = asyncio.create_task(self._reconcile_loop())
            attempt = 0
            try:
                while True:
                    started = time.time()
                    try:
                        await self._session()
                        self.status.note("socket closed by server")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # any failure -> reconnect
                        self.status.last_error = f"{type(e).__name__}: {e}"[:300]
                        self.status.note(f"socket error: {self.status.last_error}")
                    self.status.reconnects += 1
                    self.status.connected_since = None
                    attempt = 0 if time.time() - started > 60 else attempt + 1
                    delay = backoff_delay(attempt)
                    self.status.note(f"reconnecting in {delay:.1f}s (attempt {attempt + 1})")
                    await self._poll_for(delay)
            finally:
                reconciler.cancel()

    # ----------------------------------------------------------- session
    async def _session(self) -> None:
        cfg = self.cfg
        self.status.state = "connecting"
        async with websockets.connect(
            cfg.ws_url, origin=ORIGIN, user_agent_header=UA, open_timeout=10,
            ping_interval=None,  # we run our own heartbeat below so we know *when* it last succeeded
            close_timeout=2, max_size=2 ** 22,
        ) as ws:
            sub = protocol.subscribe_message(cfg)
            sent_at = time.time()
            await ws.send(protocol.encode(sub))

            acked = asyncio.Event()
            ready = {"value": False}
            buffer: List[protocol.Update] = []

            async def reader() -> None:
                async for raw in ws:
                    now = time.time()
                    self.status.last_message_at = now
                    try:
                        msg = protocol.decode(raw)
                    except protocol.ProtocolError as e:
                        self.book.stats["invalid_rows"] += 1
                        log.warning("undecodable frame: %s", e)
                        continue
                    if msg.sub_id != sub["id"]:
                        continue
                    if msg.kind == "subscribed":
                        if msg.server_ts:
                            self.latency.set_dk_clock(sent_at, now, msg.server_ts)
                        acked.set()
                    elif msg.kind == "update":
                        try:
                            upd = protocol.parse_update("events", msg, now)
                        except protocol.ProtocolError as e:
                            self.book.stats["invalid_rows"] += 1
                            log.warning("bad update: %s", e)
                            continue
                        if upd.problems:
                            log.warning("update problems: %s", upd.problems[:3])
                        if ready["value"]:
                            self._apply(upd)
                        else:
                            buffer.append(upd)
                    elif msg.error is not None:
                        raise RuntimeError(f"server error on subscription: {msg.error!r}")

            reader_task = asyncio.create_task(reader())
            heartbeat_task = asyncio.create_task(self._heartbeat(ws))
            try:
                ack_task = asyncio.create_task(acked.wait())
                await asyncio.wait({ack_task, reader_task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
                if not acked.is_set():
                    ack_task.cancel()
                    if reader_task.done():
                        reader_task.result()  # surfaces the real error
                    raise RuntimeError("subscription not acknowledged within 10s")

                # Base state. We subscribed first, so nothing can fall in the gap:
                # REST goes in, then every socket update received since goes on top.
                await self._load_base_state(reconnecting=self.status.reconnects > 0)
                for upd in buffer:
                    self._apply(upd)
                buffer.clear()
                ready["value"] = True  # no await between draining and flipping: no gap

                now = time.time()
                self.status.state = "live"
                self.status.connected_since = now
                self.status.last_confirmed_at = now
                self.status.note(f"live: {len(self.book.games)} games from REST (proxy: {cfg.proxy_label}), "
                                 f"board source: {self.status.snapshot}, subscribed to {cfg.site_name}")
                done, _ = await asyncio.wait({reader_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    t.result()  # re-raise the reason we stopped
            finally:
                reader_task.cancel()
                heartbeat_task.cancel()

    async def _load_base_state(self, reconnecting: bool) -> None:
        try:
            snap, _ = await snapshot.fetch(self.client, self.cfg)
        except Exception as e:
            first = self.status.snapshot != "browser"
            self.status.snapshot = "browser"
            err = str(e).replace(self.cfg.http_proxy, "<proxy>") if self.cfg.http_proxy else str(e)
            self.status.snapshot_error = f"{type(e).__name__}: {err}"[:200]
            if first:
                self.status.note(f"REST snapshot unavailable from this server ({self.status.snapshot_error}); "
                                 "browsers will load the board directly, socket changes still stream")
                self.publish([{"type": "board"}])  # open pages switch to loading the board themselves
            if reconnecting:
                # Socket-only: whatever we held may have missed changes during the
                # gap. Forget it and have browsers reload the board from REST.
                self.book.clear()
                self.publish([{"type": "resync"}])
            return
        self.status.snapshot, self.status.snapshot_error = "server", None
        self.status.last_snapshot_at = time.time()
        self.publish(self.book.load_snapshot(snap, mode="replace"))

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            sent = time.time()
            pong = await ws.ping()
            try:
                await asyncio.wait_for(pong, timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                raise RuntimeError(f"no pong within {HEARTBEAT_SECONDS:.0f}s")
            now = time.time()
            self.status.ping_ms = round((now - sent) * 1000, 1)
            if self.status.state == "live":
                self.status.last_confirmed_at = now

    def _apply(self, upd: protocol.Update) -> None:
        events = self.book.apply(upd)
        if events:
            self.latency.record_update(upd.created, upd.published, upd.server_ts, upd.received)
            self.publish(events)
        if self.status.state == "live":
            self.status.last_confirmed_at = upd.received
        if self.book.needs_resync:
            self._resync.set()

    # ------------------------------------------------------- reconcile
    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._resync.wait(), timeout=self.cfg.reconcile_seconds)
            except asyncio.TimeoutError:
                pass
            self._resync.clear()
            if self.status.state != "live":
                continue
            if self.status.snapshot == "browser" and self.book.needs_resync:
                self.book.needs_resync = False
                self.publish([{"type": "resync"}])  # e.g. a new game: browsers reload the board
            try:
                snap, _ = await snapshot.fetch(self.client, self.cfg)
                if self.status.snapshot == "browser":
                    self.status.note("REST snapshot reachable again; the server owns the board")
                    self.status.snapshot, self.status.snapshot_error = "server", None
                before = self.book.stats["drift_fixed"]
                self.publish(self.book.load_snapshot(snap, mode="reconcile"))
                self.status.last_reconcile_at = self.status.last_snapshot_at = time.time()
                fixed = self.book.stats["drift_fixed"] - before
                if fixed:
                    self.status.note(f"reconcile corrected {fixed} values the socket missed")
            except Exception as e:
                if self.status.snapshot != "browser":
                    log.warning("reconcile failed: %r", e)

    # --------------------------------------------------------- fallback
    async def _poll_for(self, seconds: float) -> None:
        """While the socket is down, keep the board fresh from REST."""
        deadline = time.time() + seconds
        while True:
            try:
                snap, fetched_at = await snapshot.fetch(self.client, self.cfg)
                self.publish(self.book.load_snapshot(snap, mode="replace"))
                self.status.state = "polling"
                self.status.last_snapshot_at = time.time()
                self.status.last_confirmed_at = fetched_at
            except Exception as e:
                self.status.state = "down"
                self.status.last_error = f"REST: {type(e).__name__}: {e}"[:300]
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self.cfg.poll_seconds, remaining))
