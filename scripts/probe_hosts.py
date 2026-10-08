"""Which DraftKings hosts serve the NFL board from *this* machine?

From AWS, sportsbook-nash.draftkings.com returns 403 (Akamai) while the socket
host works. This tries the same data on the other hosts listed in DraftKings'
own page config (productConfig), and prints status / size / a preview.

    python scripts/probe_hosts.py
"""
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.config import Config  # noqa: E402

cfg = Config()
q = lambda s: quote(s, safe="")  # noqa: E731
events_q = cfg.events_filter + " and tags/any(t: t eq 'OSB')"
markets_q = cfg.markets_filter + " and tags/any(t: t eq 'OSB')"
nash_path = cfg.snapshot_url.split("draftkings.com", 1)[1]
st, site = cfg.state.lower(), cfg.site_name
sd_qs = f"query={q(events_q)}&includeMarkets={q(markets_q)}&projection=sportsbook&locale=en"

CANDIDATES = [
    ("nash (current)", cfg.snapshot_url),
    ("nash-us<state> same path", f"https://sportsbook-nash-us{st}.draftkings.com{nash_path}"),
    ("ws host same path", f"https://sportsbook-ws-us-{st}.draftkings.com{nash_path}"),
    ("ws host sportsdata v2 events", f"https://sportsbook-ws-us-{st}.draftkings.com/{site}/sportsdata/v2/events?{sd_qs}"),
    ("ws host sportsdata v1 events", f"https://sportsbook-ws-us-{st}.draftkings.com/{site}/sportsdata/v1/events?{sd_qs}"),
    ("ws host sportsdata v2 events (no OSB)", f"https://sportsbook-ws-us-{st}.draftkings.com/{site}/sportsdata/v2/events?query={q(cfg.events_filter)}&includeMarkets={q(cfg.markets_filter)}"),
]

with httpx.Client(headers=cfg.http_headers, timeout=15, follow_redirects=False) as c:
    for name, url in CANDIDATES:
        try:
            r = c.get(url)
            body = r.text[:160].replace("\n", " ")
            print(f"{r.status_code}  {len(r.content):>7}B  {r.headers.get('server', '-'):<14} {name}\n      {body}")
        except Exception as e:
            print(f"ERR  {name}: {e!r}")
