"""Small, strict parsers shared by the REST and socket paths.

DraftKings data is untrusted input. Each helper returns None when a value
doesn't look right, and the caller drops that one row instead of crashing.
"""
import re
from datetime import datetime, timezone
from typing import Optional

_AMERICAN = re.compile(r"[+-]?\d{3,6}")
_ISO_FRACTION = re.compile(r"(\.\d{1,6})\d*")


def american(value) -> Optional[int]:
    """'+350' / '−455' / 'EVEN' -> int. DraftKings uses a unicode minus sign."""
    if value is None:
        return None
    s = str(value).strip().replace("−", "-")
    if s.upper() in ("EVEN", "EV"):
        return 100
    if not _AMERICAN.fullmatch(s):
        return None
    n = int(s)
    return n if abs(n) >= 100 else None


def decimal_odds(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 1.0 else None


def points(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def iso_ts(value) -> Optional[float]:
    """ISO-8601 -> unix seconds. Handles 'Z' and 7-digit fractions on Python 3.10."""
    if not value:
        return None
    s = _ISO_FRACTION.sub(r"\1", str(value).strip()).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# Market types we keep. betOfferTypeId is stable across sports
# (NFL "Spread" and baseball "Run Line" are both 1), so a second league
# mostly works without new mapping code.
BET_OFFER_TYPES = {2: "moneyline", 1: "spread", 6: "total"}
MARKET_NAMES = {"moneyline": "moneyline", "spread": "spread", "total": "total"}


def market_kind(bet_offer_type_id, name) -> Optional[str]:
    try:
        kind = BET_OFFER_TYPES.get(int(bet_offer_type_id))
    except (TypeError, ValueError):
        kind = None
    return kind or MARKET_NAMES.get(str(name or "").strip().lower())


SIDES = {"home": "home", "away": "away", "over": "over", "under": "under"}


def side(outcome_type) -> Optional[str]:
    return SIDES.get(str(outcome_type or "").strip().lower())
