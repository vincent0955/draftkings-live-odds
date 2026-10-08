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


# DraftKings selection ids encode the market, side and line of a main-line
# selection, which lets the server place a socket update without a snapshot:
#   0ML84695570_3        -> market 1_84695570, away (moneyline)
#   0HC84695450N300_1    -> market 2_84695450, home, -3.0 (spread)
#   0OU84695570O4750_1   -> market 3_84695570, over, 47.5 (total)
# Suffix _1 is home/over, _3 is away/under. Verified against every main-line
# selection in the captured snapshots (tests/test_snapshot.py).
_SEL_ML = re.compile(r"0ML(\d+)_([13])")
_SEL_HC = re.compile(r"0HC(\d+)([NP])(\d+)_([13])")
_SEL_OU = re.compile(r"0OU(\d+)([OU])(\d+)_([13])")
MARKET_PREFIX_KIND = {"1": "moneyline", "2": "spread", "3": "total"}


def selection_key(selection_id):
    """-> (market_id, side, line) or None if the id isn't a main-line selection."""
    s = str(selection_id or "")
    m = _SEL_ML.fullmatch(s)
    if m:
        return f"1_{m.group(1)}", "home" if m.group(2) == "1" else "away", None
    m = _SEL_HC.fullmatch(s)
    if m:
        line = int(m.group(3)) / 100 * (-1 if m.group(2) == "N" else 1)
        return f"2_{m.group(1)}", "home" if m.group(4) == "1" else "away", line
    m = _SEL_OU.fullmatch(s)
    if m:
        return f"3_{m.group(1)}", "over" if m.group(2) == "O" else "under", int(m.group(3)) / 100
    return None


def market_kind_from_id(market_id):
    """'2_84695450' -> 'spread'. Only for main-line market ids."""
    prefix, _, rest = str(market_id or "").partition("_")
    return MARKET_PREFIX_KIND.get(prefix) if rest.isdigit() else None
