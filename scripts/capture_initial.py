"""Capture what the socket sends when subscribing with initialData:true,
plus a REST snapshot taken at the same moment, so the two can be compared.

    python scripts/capture_initial.py            # 15s of frames, NFL game lines
Writes fixtures/ws_nfl_initial_data.json and fixtures/nfl_snapshot_at_initial.json
"""
import asyncio
import base64
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402
import websockets  # noqa: E402

from app import protocol  # noqa: E402
from app.config import ORIGIN, UA, Config  # noqa: E402


async def main(seconds: float = 15) -> None:
    cfg = Config()
    sub = protocol.subscribe_message(cfg)
    sub["params"]["queryParams"]["initialData"] = True
    frames = []
    async with websockets.connect(cfg.ws_url, origin=ORIGIN, user_agent_header=UA, max_size=2 ** 25) as ws:
        raw_sub = protocol.encode(sub)
        frames.append({"dir": "send", "t": time.time(), "raw_b64": base64.b64encode(raw_sub).decode()})
        await ws.send(raw_sub)
        async with httpx.AsyncClient() as client:
            resp = await client.get(cfg.snapshot_url, headers=cfg.http_headers, timeout=15)
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
            except asyncio.TimeoutError:
                break
            frames.append({"dir": "receive", "t": time.time(),
                           "raw_b64": base64.b64encode(raw if isinstance(raw, bytes) else raw.encode()).decode()})
    out = os.path.join(ROOT, "fixtures", "ws_nfl_initial_data.json")
    with open(out, "w") as f:
        json.dump({"url": cfg.ws_url, "note": "subscribe with initialData:true", "frames": frames}, f, indent=1)
    snap_out = os.path.join(ROOT, "fixtures", "nfl_snapshot_at_initial.json")
    with open(snap_out, "w", encoding="utf-8") as f:
        f.write(resp.text)
    received = [f for f in frames if f["dir"] == "receive"]
    print(f"REST {resp.status_code} ({len(resp.content)} bytes)")
    print(f"saved {len(received)} received frames, largest {max((len(f['raw_b64']) for f in received), default=0)} b64 chars")
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
