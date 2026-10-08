"""Settings, all overridable with environment variables."""
import os
from dataclasses import dataclass, field
from urllib.parse import quote

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
ORIGIN = "https://sportsbook.draftkings.com"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # DraftKings serves a separate site per US state. The state appears in the
    # REST path (US-IL-SB), the socket host (sportsbook-ws-us-il) and the
    # subscribe message (dkusil). Odds can differ slightly between states.
    state: str = field(default_factory=lambda: _env("DK_STATE", "IL").upper())
    ws_region: str = field(default_factory=lambda: _env("DK_WS_REGION", "").lower())
    league_id: str = field(default_factory=lambda: _env("DK_LEAGUE_ID", "88808"))  # NFL
    subcategory_id: str = field(default_factory=lambda: _env("DK_SUBCATEGORY_ID", "4518"))  # Game Lines

    # "live" connects to DraftKings. "replay" plays recorded frames. "simulate"
    # generates fake NFL moves in DraftKings' wire format.
    source: str = field(default_factory=lambda: _env("SOURCE", "live"))
    replay_speed: float = field(default_factory=lambda: float(_env("REPLAY_SPEED", "1")))

    # Point at a fake DraftKings in integration tests.
    ws_url_override: str = field(default_factory=lambda: _env("DK_WS_URL", ""))
    snapshot_url_override: str = field(default_factory=lambda: _env("DK_SNAPSHOT_URL", ""))

    # Optional proxy for the REST snapshot only (http://user:pass@host:port or
    # socks5://...). DraftKings' CDN blocks REST from cloud IPs; the socket is
    # not blocked and always connects directly. Never logged.
    http_proxy: str = field(default_factory=lambda: _env("DK_HTTP_PROXY", ""), repr=False)

    reconcile_seconds: float = field(default_factory=lambda: float(_env("RECONCILE_SECONDS", "60")))
    poll_seconds: float = field(default_factory=lambda: float(_env("POLL_SECONDS", "5")))
    ping_interval: float = 10.0
    ping_timeout: float = 10.0
    http_timeout: float = 10.0

    @property
    def proxy_label(self) -> str:
        """Proxy host without credentials, safe to show."""
        if not self.http_proxy:
            return "none"
        return self.http_proxy.split("@")[-1].split("://")[-1]

    @property
    def site_code(self) -> str:
        return f"US-{self.state}-SB"

    @property
    def site_name(self) -> str:
        return f"dkus{self.state.lower()}"

    @property
    def ws_url(self) -> str:
        if self.ws_url_override:
            return self.ws_url_override
        region = self.ws_region or self.state.lower()
        return f"wss://sportsbook-ws-us-{region}.draftkings.com/websocket?format=msgpack&locale=en"

    @property
    def events_filter(self) -> str:
        return (
            f"$filter=leagueId eq '{self.league_id}' AND "
            f"clientMetadata/Subcategories/any(s: s/Id eq '{self.subcategory_id}')"
        )

    @property
    def markets_filter(self) -> str:
        return (
            f"$filter=clientMetadata/subCategoryId eq '{self.subcategory_id}' AND "
            "tags/all(t: t ne 'SportcastBetBuilder')"
        )

    @property
    def snapshot_url(self) -> str:
        if self.snapshot_url_override:
            return self.snapshot_url_override
        # Built by hand so spaces become %20, exactly like the browser sends.
        params = [
            ("isBatchable", "false"),
            ("templateVars", self.league_id),
            ("eventsQuery", self.events_filter),
            ("marketsQuery", self.markets_filter),
            ("include", "Events"),
            ("entity", "events"),
        ]
        qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
        return (
            f"https://sportsbook-nash.draftkings.com/sites/{self.site_code}"
            f"/api/sportscontent/controldata/league/leagueSubcategory/v1/markets?{qs}"
        )

    @property
    def http_headers(self) -> dict:
        return {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": ORIGIN,
            "Referer": ORIGIN + "/",
        }
