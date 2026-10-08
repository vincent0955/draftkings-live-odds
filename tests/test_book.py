from app import protocol, wire
from app.book import BOARD, Book
from app.snapshot import normalize
from tests.conftest import recorded_frames, subscriptions

TENNIS = "ws_live_tennis_match.json"
MATCH_ML = "1_86639010"


def apply_frame(book, raw, entity="events", received=None):
    msg = protocol.decode(raw)
    upd = protocol.parse_update(entity, msg, received=received or msg.server_ts)
    return book.apply(upd, now=upd.received)


def book_from(data):
    b = Book()
    b.load_snapshot(normalize(data))
    return b


def test_replaying_a_live_match_ends_on_the_last_prices(tennis_snapshot):
    """Snapshot + 5 minutes of real socket frames = the prices DraftKings showed last."""
    book = book_from(tennis_snapshot)
    subs = subscriptions(TENNIS)
    last_price, last_susp, susp_flips = {}, None, 0
    for direction, t, raw in recorded_frames(TENNIS):
        if direction != "receive":
            continue
        msg = protocol.decode(raw)
        p = subs.get(msg.sub_id)
        if msg.kind != "update" or "6364" not in p["queryParams"]["query"]:
            continue  # the game-lines subscription only (the page held overlapping ones)
        upd = protocol.parse_update(p["entity"], msg, t)
        for op in upd.ops:
            if op.entity == "selection" and op.action == "change":
                last_price[op.id] = op.fields["american"]
            if op.entity == "market" and op.id == MATCH_ML and op.fields.get("suspended") is not None:
                last_susp = op.fields["suspended"]
        for ev in book.apply(upd, now=t):
            susp_flips += ev.get("type") == "market"

    held = {o.selection_id: o.american for o in book.outcomes.values() if o.market_id == MATCH_ML}
    assert held == {sid: last_price[sid] for sid in held}
    assert book.markets[MATCH_ML].suspended is last_susp
    assert susp_flips >= 10  # suspended/reopened around points
    assert book.stats["invalid_rows"] == 0 and book.stats["unknown_ops"] == 0


def test_line_move_is_delete_plus_insert_and_keeps_the_row(nfl_day1):
    """SEA spread -3 -> -2.5, exactly as it happened between the two captures."""
    book = book_from(nfl_day1)
    market = "2_84695450"
    assert book.outcomes[(market, "home")].line == -3

    raw = wire.update_frame(
        "sub", published=100,
        delete_selections=["0HC84695450N300_1", "0HC84695450P300_3"],
        insert_selections=[
            wire.selection_full("0HC84695450N250_1", market, "SEA Seahawks", -122, -2.5, "Home"),
            wire.selection_full("0HC84695450P250_3", market, "SF 49ers", 102, 2.5, "Away"),
        ],
    )
    events = apply_frame(book, raw)

    home = book.outcomes[(market, "home")]
    assert (home.selection_id, home.line, home.american) == ("0HC84695450N250_1", -2.5, -122)
    assert (home.prev_line, home.prev_american) == (-3, 100)
    assert home.available
    assert "0HC84695450N300_1" not in book.sel_index
    assert book.sel_index["0HC84695450N250_1"] == (market, "home")
    assert {e["side"] for e in events if e["type"] == "outcome"} == {"home", "away"}

    # Later price-only updates now land on the new id.
    apply_frame(book, wire.update_frame("sub", published=101, change_selections=[
        wire.selection_change("0HC84695450N250_1", "SEA Seahawks", -118, -2.5)]))
    assert (home.american, home.prev_american) == (-118, -122)


def test_deleted_line_shows_unavailable_until_replaced(nfl_day1):
    book = book_from(nfl_day1)
    apply_frame(book, wire.update_frame("sub", published=100, delete_selections=["0HC84695450N300_1"]))
    out = book.outcomes[("2_84695450", "home")]
    assert out.available is False and out.american == 100  # last price kept for display


def test_out_of_order_updates_are_dropped(nfl_day1):
    book = book_from(nfl_day1)
    sid = "0ML84695570_3"  # TB moneyline
    apply_frame(book, wire.update_frame("sub", published=200, change_selections=[wire.selection_change(sid, "TB", 360)]))
    apply_frame(book, wire.update_frame("sub", published=150, change_selections=[wire.selection_change(sid, "TB", 340)]))
    assert book.outcomes[("1_84695570", "away")].american == 360
    assert book.stats["dropped_out_of_order"] == 1


def test_suspension(nfl_day1):
    book = book_from(nfl_day1)
    ev = apply_frame(book, wire.update_frame("sub", published=100, change_markets=[wire.market_change("1_84695570", "Moneyline", True)]))
    assert book.markets["1_84695570"].suspended is True
    assert ev == [{"type": "market", "game_id": "34118137", "market": "moneyline", "market_id": "1_84695570", "suspended": True}]


def test_malformed_update_counted_not_crashed(nfl_day1):
    book = book_from(nfl_day1)
    bad = [24, ["0ML84695570_3", "TB", ["not odds"], "x", None, 0, [], None]]
    apply_frame(book, wire.update_frame("sub", published=100, change_selections=[bad]))
    assert book.outcomes[("1_84695570", "away")].american == 350
    assert book.stats["invalid_rows"] == 1


def test_game_end_removes_game(nfl_day1):
    book = book_from(nfl_day1)
    ev = apply_frame(book, wire.update_frame("sub", published=100, delete_events=["34118137"]))
    assert ev == [BOARD]
    assert "34118137" not in book.games
    assert not any(m.game_id == "34118137" for m in book.markets.values())
    assert "0ML84695570_3" not in book.sel_index


def test_reconcile_needs_two_strikes(nfl_day1, nfl_day2):
    """Day 1 -> day 2 differs on 52 outcomes (9 line moves, 34 price moves).
    A single REST read can be stale, so the first reconcile only flags them."""
    book = book_from(nfl_day1)
    day2 = normalize(nfl_day2)

    assert book.load_snapshot(day2, mode="reconcile") == [BOARD]  # 14 new games appear
    sea = book.outcomes[("2_84695450", "home")]
    assert sea.line == -3 and book.stats["drift_fixed"] == 0

    # The socket updates one of the flagged values in between (PHI ML, +230 on
    # day 1, +280 on day 2): the socket is fresher, so leave it alone.
    apply_frame(book, wire.update_frame("sub", published=15, change_selections=[
        wire.selection_change("0ML84695697_3", "PHI Eagles", 260)]))

    events = book.load_snapshot(day2, mode="reconcile")
    assert sea.line != -2.5  # the old object was replaced...
    assert book.outcomes[("2_84695450", "home")].line == -2.5  # ...by the confirmed REST value
    assert book.stats["drift_fixed"] == 51
    assert book.outcomes[("1_84695697", "away")].american == 260
    assert len(events) == 51


def test_socket_beats_snapshot_and_polling_beats_socket(nfl_day1):
    """Reconnect order: REST loads first, buffered socket updates go on top.
    While the socket is down, polling REST wins outright."""
    book = book_from(nfl_day1)
    apply_frame(book, wire.update_frame("sub", published=50, change_selections=[
        wire.selection_change("0ML84695570_3", "TB", 345)]))
    assert book.outcomes[("1_84695570", "away")].american == 345
    book.load_snapshot(normalize(nfl_day1), mode="replace")  # e.g. polling during an outage
    assert book.outcomes[("1_84695570", "away")].american == 350
    # A socket update stamped long ago by DraftKings' clock still applies on
    # top of REST: we never compare their clock with ours.
    apply_frame(book, wire.update_frame("sub", published=1, change_selections=[
        wire.selection_change("0ML84695570_3", "TB", 360)]))
    assert book.outcomes[("1_84695570", "away")].american == 360


def test_board_shape(nfl_day2):
    book = book_from(nfl_day2)
    board = book.board()
    assert len(board) == 29
    first = board[0]
    assert first["name"] == "TB Buccaneers @ DAL Cowboys"
    assert set(first["markets"]) == {"moneyline", "spread", "total"}
    assert set(first["markets"]["total"]["outcomes"]) == {"over", "under"}
