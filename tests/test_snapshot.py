import copy

import pytest

from app import parse
from app.snapshot import normalize


def test_real_nfl_snapshot_has_three_main_markets_per_game(nfl_day2):
    snap = normalize(nfl_day2)
    assert (len(snap.games), len(snap.markets), len(snap.selections)) == (29, 87, 174)
    assert snap.dropped == []
    kinds = {}
    for m in snap.markets.values():
        kinds.setdefault(m.game_id, set()).add(m.kind)
    assert all(k == {"moneyline", "spread", "total"} for k in kinds.values())


def test_values_match_draftkings(nfl_day2):
    snap = normalize(nfl_day2)
    game = next(g for g in snap.games.values() if g.name == "TB Buccaneers @ DAL Cowboys")
    assert (game.away, game.home) == ("TB Buccaneers", "DAL Cowboys")
    rows = {(snap.markets[s.market_id].kind, s.side): s for s in snap.selections
            if snap.markets[s.market_id].game_id == game.id}
    assert rows[("moneyline", "away")].american == 350
    assert rows[("moneyline", "home")].american == -455
    assert (rows[("spread", "away")].line, rows[("spread", "away")].american) == (8.5, -108)
    assert (rows[("total", "over")].line, rows[("total", "over")].american) == (47.5, -115)


def test_bad_rows_are_dropped_not_fatal(nfl_day1):
    data = copy.deepcopy(nfl_day1)
    sels = data["selections"]
    sels[0]["displayOdds"]["american"] = "abc"          # garbage odds
    sels[1].pop("outcomeType")                           # missing side
    spread = next(s for s in sels if s["id"].startswith("0HC"))
    spread.pop("points")                                 # spread with no line
    data["markets"].append({"id": "x", "eventId": "nope", "marketType": {"betOfferTypeId": 2}})
    data["markets"].append({"id": "y", "eventId": data["events"][0]["id"], "name": "Alt Spread",
                            "marketType": {"betOfferTypeId": 99, "name": "Alt Spread"}})
    del data["events"][1]["participants"]                # malformed event still kept by id

    snap = normalize(data)
    assert len(snap.selections) == 90 - 3
    assert len(snap.dropped) == 5
    assert "x" not in snap.markets and "y" not in snap.markets


def test_not_a_dict_raises():
    with pytest.raises(ValueError):
        normalize(["unexpected"])


def test_parsers():
    assert parse.american("−455") == -455
    assert parse.american("+350") == 350
    assert parse.american("EVEN") == 100
    assert parse.american("+50") is None and parse.american("") is None and parse.american(None) is None
    assert parse.iso_ts("2026-10-09T00:15:00.0000000Z") == 1791504900.0
    assert parse.iso_ts("2026-10-07T13:00:09.095Z") == pytest.approx(1791378009.095)
    assert parse.market_kind(1, "Run Line") == "spread"  # baseball works without new mapping
