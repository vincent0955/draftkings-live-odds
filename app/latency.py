"""Rolling latency stats. Each series keeps the last N samples in milliseconds."""
from collections import deque
from typing import Dict


class Latency:
    # dk_internal:       DraftKings createdTime -> publishedTime (their pipeline)
    # dk_to_server:      publishedTime -> our server received it (DK clock offset corrected)
    # wire:              DraftKings socket send timestamp -> our server received it (corrected)
    # server_to_browser: our server emitted -> browser received (browser clock offset corrected)
    SERIES = ("dk_internal", "dk_to_server", "wire", "server_to_browser")

    def __init__(self, maxlen: int = 2000) -> None:
        self.data: Dict[str, deque] = {name: deque(maxlen=maxlen) for name in self.SERIES}
        # DraftKings' clock minus ours, estimated NTP-style from the subscribe
        # round trip (their ack carries their timestamp). Error is at most rtt/2.
        self.dk_clock_offset = 0.0
        self.dk_clock_rtt: float = None

    def set_dk_clock(self, sent_at: float, ack_at: float, dk_ts: float) -> None:
        self.dk_clock_rtt = ack_at - sent_at
        self.dk_clock_offset = dk_ts - (sent_at + ack_at) / 2

    def add(self, name: str, ms: float) -> None:
        if name in self.data and ms == ms:  # skip NaN
            self.data[name].append(float(ms))

    def record_update(self, created, published, sent, received) -> None:
        """DraftKings timestamps are converted to our clock before subtracting."""
        off = self.dk_clock_offset
        if created and published:
            self.add("dk_internal", (published - created) * 1000)  # same clock, no correction
        if published:
            self.add("dk_to_server", (received - (published - off)) * 1000)
        if sent:
            self.add("wire", (received - (sent - off)) * 1000)

    def summary(self) -> dict:
        out = {}
        for name, values in self.data.items():
            if not values:
                out[name] = {"n": 0}
                continue
            s = sorted(values)
            pick = lambda q: round(s[min(len(s) - 1, int(q * len(s)))], 1)  # noqa: E731
            out[name] = {"n": len(s), "p50": pick(0.5), "p95": pick(0.95), "max": round(s[-1], 1)}
        out["dk_clock"] = {
            "offset_ms": round(self.dk_clock_offset * 1000, 1),
            "uncertainty_ms": None if self.dk_clock_rtt is None else round(self.dk_clock_rtt * 500, 1),
        }
        return out
