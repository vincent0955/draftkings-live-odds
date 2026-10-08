"""Can this machine reach DraftKings? Run it on the server before deploying.

    python scripts/probe.py                 # IL site, 60s of socket
    python scripts/probe.py --states IL,NJ,VA --listen 120

Checks, per state:
  1. REST snapshot: HTTP status, size, games found, response time
  2. Socket: connects, subscription acknowledged, updates received
Exit code 0 if at least one state fully works.
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
import websockets  # noqa: E402

from app import protocol, snapshot  # noqa: E402
from app.config import ORIGIN, UA, Config  # noqa: E402


async def probe_rest(cfg: Config) -> bool:
    print(f"\n[{cfg.state}] REST via proxy {cfg.proxy_label}: {cfg.snapshot_url[:90]}...")
    t = time.time()
    try:
        async with httpx.AsyncClient(http2=False, proxy=cfg.http_proxy or None) as client:
            resp = await client.get(cfg.snapshot_url, headers=cfg.http_headers, timeout=15)
    except Exception as e:
        print(f"  FAIL  request error: {e!r}")
        return False
    ms = (time.time() - t) * 1000
    print(f"  HTTP {resp.status_code}  {len(resp.content)} bytes  {ms:.0f} ms  server={resp.headers.get('server')}")
    if resp.status_code != 200:
        print(f"  FAIL  body starts: {resp.text[:200]!r}")
        return False
    try:
        snap = snapshot.normalize(resp.json())
    except Exception as e:
        print(f"  FAIL  not the expected JSON: {e!r}")
        return False
    print(f"  OK    {len(snap.games)} games, {len(snap.markets)} markets, {len(snap.selections)} selections")
    for g in list(snap.games.values())[:3]:
        print(f"        {g.name}")
    return bool(snap.games)


async def probe_ws(cfg: Config, listen: float) -> bool:
    print(f"[{cfg.state}] socket {cfg.ws_url}")
    try:
        async with websockets.connect(cfg.ws_url, origin=ORIGIN, user_agent_header=UA,
                                      open_timeout=10, ping_interval=10, ping_timeout=10) as ws:
            sub = protocol.subscribe_message(cfg)
            await ws.send(protocol.encode(sub))
            acked, updates, ops = False, 0, 0
            end = time.time() + listen
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                except asyncio.TimeoutError:
                    break
                msg = protocol.decode(raw)
                if msg.kind == "subscribed":
                    acked = True
                    print(f"  OK    subscribed (server ts {msg.server_ts:.3f}, local {time.time():.3f})")
                elif msg.kind == "update":
                    upd = protocol.parse_update("events", msg, time.time())
                    updates += 1
                    ops += len(upd.ops)
                    if updates <= 5:
                        lag = (upd.received - upd.published) * 1000 if upd.published else float("nan")
                        print(f"        update: {[(o.action, o.entity, o.id) for o in upd.ops][:3]}  publish->here {lag:.0f} ms")
                else:
                    print(f"        {msg.kind}: {str(msg.payload)[:120]} error={msg.error}")
            print(f"  {'OK  ' if acked else 'FAIL'}  acked={acked}, {updates} updates / {ops} changes in {listen:.0f}s, "
                  f"ping {ws.latency * 1000:.0f} ms")
            if acked and updates == 0:
                print("        (no updates is normal pregame; lines may not move in a short window)")
            return acked
    except Exception as e:
        print(f"  FAIL  {e!r}")
        return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", default=os.environ.get("DK_STATE", "IL"))
    ap.add_argument("--listen", type=float, default=60)
    args = ap.parse_args()
    ok_any = False
    for st in [s.strip().upper() for s in args.states.split(",") if s.strip()]:
        cfg = Config(state=st)
        rest = await probe_rest(cfg)
        ws = await probe_ws(cfg, args.listen)
        ok_any |= rest and ws
        print(f"[{st}] RESULT: REST {'ok' if rest else 'BLOCKED/FAILED'}, socket {'ok' if ws else 'BLOCKED/FAILED'}")
    return 0 if ok_any else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
