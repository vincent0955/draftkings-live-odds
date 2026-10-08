# DraftKings NFL Live Odds

Pulls NFL main lines (moneyline, spread, total) from DraftKings and shows them on a page that updates by itself as lines move.

- Live: https://54-165-170-102.sslip.io (AWS EC2, us-east-1)
- Health: `/health` (feed state, last confirmed time, reconnects, latency)

## Run it locally

Needs Python 3.10+. DraftKings only serves US visitors, so outside the US you need a US VPN for `SOURCE=live`.

```
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\pip ...
.venv/bin/python -m app                            # http://localhost:8000
```

Other modes, no DraftKings access needed:

```
SOURCE=replay   python -m app    # replays 5 min of a real recorded live match (DraftKings' original bytes)
SOURCE=simulate python -m app    # fake NFL moves, suspensions and line moves, in DraftKings' wire format
python -m pytest                 # 33 tests, all against captured DraftKings data or a fake DraftKings server
python scripts/probe.py --states IL,NJ   # can this machine reach DraftKings?
```

On Windows PowerShell set env vars with `$env:SOURCE="replay"` first.

## How it works

```
DraftKings socket (msgpack, diffs) ──> server: odds book in memory ──SSE──> browsers
DraftKings REST (full snapshot) ─────> the server, if DraftKings lets it
                                  └──> otherwise each browser, directly (see "What happened on AWS")
```

1. The server opens one WebSocket to DraftKings and subscribes to NFL game lines, using the same subscription DraftKings' own NFL page sends.
2. It then loads the full board from DraftKings' REST endpoint, and applies every socket update received since subscribing on top. Subscribing first means nothing can fall in the gap.
3. From then on, each socket update changes only the affected prices in memory and is pushed to every open browser over Server-Sent Events.
4. Every 60 seconds it re-fetches REST as a safety net and counts any value the socket missed ("drift").

The page is plain HTML and JS. It gets the full board on connect and then only changes.

On AWS, step 2 is different, because DraftKings blocks the REST call from AWS IPs. The browser loads the board from DraftKings itself, and the server streams the socket changes on top. Details below.

## Why this approach

**Polling vs the socket.** DraftKings' page gets its odds from a WebSocket that pushes changes within milliseconds. Polling REST instead would leave numbers on screen up to one poll interval old, and REST is CDN-cached for 1 second, so even fast polling has a floor. The socket is the lowest-latency source available, so the app uses it, with REST as the base state and the fallback.

**SSE to the browser** instead of WebSockets: updates only go one way, SSE reconnects by itself, and it works through any proxy. No frontend framework needed.

**Hosting.** The server needs to hold a connection open all day, which rules out serverless (Vercel functions end after each request). It runs as one small always-on EC2 instance (t3.small, us-east-1) behind Caddy for HTTPS. `deploy/user-data.sh` sets it up unattended on first boot: packages, an NTP-synced clock, a read-only deploy key for this repo, the probe, the service and HTTPS.

## What I found in DraftKings' traffic

I recorded the DraftKings site with Chrome DevTools (through a US VPN, since I'm in Japan), then reverse-engineered both feeds from the captures in `fixtures/`.

**REST snapshot.** `sportsbook-nash.draftkings.com/sites/US-IL-SB/api/sportscontent/controldata/league/leagueSubcategory/v1/markets`, with league `88808` (NFL) and subcategory `4518` (Game Lines). Returns exactly the main lines, as separate `events`, `markets` and `selections` lists joined by ID. Fields that are false are left out entirely (`isSuspended` only appears when true).

**Socket.** `wss://sportsbook-ws-us-il.draftkings.com/websocket?format=msgpack`. Binary MessagePack, JSON-RPC style. Subscriptions use `initialData: false`, so it only sends changes, never the full state. That is why REST is needed at all.

Each update has three groups: inserts, deletes, changes. Objects are positional arrays with no field names and a type tag (`24` = selection change, `35` = new selection, `23` = market change, and so on). I mapped the positions from about 700 live messages. Every field is validated, and anything that doesn't fit is dropped and counted rather than crashing anything.

**The line is part of the selection ID.** `0HC84695450N300_1` is SEA -3. When that spread moved to -2.5 overnight, DraftKings deleted that selection and created `0HC84695450N250_1`. Price-only moves keep the ID. So the app keys every price by (market, side), never by selection ID, and shows "was -3" next to the new line. I confirmed this by comparing two NFL snapshots a day apart: all 18 IDs that disappeared were line moves.

**Suspensions.** In live play a market is flagged suspended for a second or more around each point, then reopens with new prices. The page shows SUSP instead of a price that can't be bet.

**REST can lag the socket.** During the live capture, REST showed one player at +115 three times over 9 seconds, while the socket said -146 two seconds later with no point played in between. So the minute-by-minute safety check only corrects a value if REST disagrees twice in a row and the socket hasn't touched it in between.

**Clocks.** My laptop's clock was 1.7s behind DraftKings' one day and 0.4s the next. So the app never compares DraftKings' timestamps with its own clock for ordering. For latency it estimates DraftKings' clock offset NTP-style from the subscribe round trip (their acknowledgement carries their timestamp) and corrects for it.

## Auth, geo and bot protection

- **Geo:** blocked from Japan. A US VPN (Windscribe) worked in the browser. DraftKings picked Illinois from the VPN IP, and the state is in every URL (`US-IL-SB`, `dkusil`, `ws-us-il`). Odds can differ slightly by state, so this app uses Illinois (configurable with `DK_STATE`).
- **Cookies / tokens:** none needed as far as I can tell. The REST response has `access-control-allow-origin: *`, which browsers only accept for requests sent without cookies. The socket "token" is the literal string `default-token`, so nothing expires.
- **Bot protection:** Akamai sits in front of the REST host and returns 403 "Access Denied" to AWS IPs. See the next section.
- **Rate limits:** the app makes one socket connection plus one REST call a minute, the same as one person with the page open.

## What happened on AWS

`scripts/probe.py` on the new EC2 instance (us-east-1):

- REST snapshot: **403 Access Denied** from `AkamaiGHost`, for the IL, NJ and VA sites alike.
- Socket: **works**, subscribed in 23 ms. The socket host isn't behind the same Akamai rule.

The same REST call worked from my laptop through Windscribe, whose servers are in data centers too, so this looks like a block on large cloud providers rather than on servers in general.

What I tried before changing the design:

1. `initialData: true` on the subscription, in five variants (events vs markets, `sportsbook` vs `betOffers` projections). The socket only ever acknowledges; it never sends a board (`fixtures/ws_nfl_initial_data.json`). DraftKings' own client code always loads the board over REST.
2. The other hosts in DraftKings' page config (`scripts/probe_hosts.py`). The socket host does answer plain HTTP from AWS (404, not 403), but I couldn't find a path on it that serves the board.

What the app does now, when the server can't get REST:

- **The browser loads the board.** DraftKings' REST API sends `access-control-allow-origin: *`, so the page fetches the board straight from DraftKings using the viewer's own connection, and refreshes it every 2 minutes (that's how new games appear).
- **The server still owns the live data.** It keeps the socket open on AWS and sends every change to browsers over SSE. Without a snapshot it doesn't know which market a selection belongs to, but the selection ID itself encodes the market, side and line (`0HC84695450N300_1` = market `2_84695450`, home, -3; `0ML..._3` = moneyline, away). I checked that decoding against all 500 selections in four captured snapshots.
- **Socket beats REST.** When a page loads, the server sends everything its socket has seen, and the page lays that over the REST copy, because the socket is always the newer of the two.
- **After a socket reconnect**, the server forgets what it held (it may have missed changes during the gap) and tells every page to reload the board from REST.
- **If REST becomes reachable from the server again**, it takes the board back and the browsers switch over by themselves.

The trade-off: a viewer whose own network is also blocked by DraftKings gets a banner saying so. When the server can reach REST (for example on my laptop), it works exactly as described in "How it works".

## How fresh are the odds?

Measured from DraftKings' own timestamps on 721 live updates in the capture:

| Stage | p50 | p95 |
|---|---|---|
| DraftKings internal (created to published) | 23 ms | 76 ms |
| DraftKings published to socket send | 34 ms | 134 ms |
| DraftKings published to our server: laptop in Japan via US VPN | 128 ms | 152 ms |
| DraftKings published to our server: AWS | TODO from deploy | |
| Our server to browser | TODO from deploy | |

First live run (Oct 7, my laptop through the VPN, pointed at the tennis league because no NFL game was on): 114 socket updates in 6.5 minutes, every one applied, with 0 invalid rows, 0 out-of-order, 0 reconnects and 0 drift corrections. DraftKings' clock was 380 ms ahead of my laptop (±99 ms), which matches what the captures showed. The same build on the NFL board matched DraftKings' page line for line.

One thing the first run taught me: browsers hold back events for background tabs, so "server to browser" came out at 42 seconds p95 while the tab was hidden. The page now only measures while it is visible.

The page footer shows these live (p50 and p95 over the last 2000 updates), and `/health` has the raw numbers. The header shows how long ago the odds were last *confirmed* current. That is different from how long ago they last moved: a line can sit still for an hour and still be current, as long as the socket is provably alive. The server pings DraftKings every 5 seconds, and the page says the odds may be stale if confirmation stops.

## When things go wrong

- **Socket drops:** reconnect with exponential backoff and jitter (1s, 2s, 4s, up to 30s). While waiting, the board is refreshed from REST every 5 seconds and the page says "polling".
- **Dead connection that never closes:** caught by the 5-second ping. No pong means reconnect.
- **DraftKings unreachable:** the page keeps the last good odds with a banner showing when they were last confirmed, and the server keeps retrying.
- **REST blocked but socket fine (AWS):** browsers load the board themselves and the server streams changes (see "What happened on AWS").
- **Unexpected data:** each row is validated on its own. A bad row is dropped and counted, never fatal. A message type we can't decode triggers an immediate REST re-sync.
- **Out-of-order updates:** dropped using DraftKings' publish timestamps.
- **Browser loses the server:** EventSource reconnects by itself, and every reconnect starts with a full board, so a browser can't drift.
- **Slow browser:** it gets a fresh full board instead of an ever-growing queue.

All of these are covered by `tests/test_feed_integration.py`, which runs the real feed against a fake DraftKings server.

## Testing without a live NFL game

There was no NFL game before the deadline, so:

- The parser and odds book are tested against real captures: NFL snapshots from two days, 717 frames from a live tennis match, and baseball games ending.
- `SOURCE=replay` plays the recorded live match through the real pipeline at real speed.
- `SOURCE=simulate` generates NFL moves in DraftKings' exact binary format, including line moves as delete plus insert.
- Pregame NFL lines still move during the week (9 lines and 34 prices changed between my two captures), so the deployed app gets real moves.

## A second sportsbook or league

**Second league:** mostly config. League and subcategory IDs are settings (`DK_LEAGUE_ID`, `DK_SUBCATEGORY_ID`), and market types are mapped by DraftKings' `betOfferTypeId`, which is the same across sports (baseball's "Run Line" maps to spread with no new code). The tennis board in replay mode runs through the same code.

**Second sportsbook:** each book becomes an adapter that outputs the same normalized events (game, market, side, line, odds) into the same book and SSE stream. The hard part is not fetching but matching: the same game and team are named differently on each book ("LA Rams" vs "Los Angeles Rams"), and lines are formatted differently. That's where AI tooling helps most: drafting a new adapter from captured traffic like I did here, building the team and market name mapping, and flagging when a book's format changes (here, a spike in the "invalid rows" counter).

## Layout

```
app/protocol.py   socket protocol: subscribe, decode, positional field mapping
app/snapshot.py   REST fetch + normalize
app/book.py       in-memory odds book: apply snapshot / updates, emit changes
app/feed.py       live loop: subscribe, snapshot, buffer, heartbeat, reconnect, reconcile
app/server.py     FastAPI: page, /api/odds, /api/stream (SSE), /health
static/index.html the page
fixtures/         real DraftKings captures used by tests and replay
scripts/          probe.py (can this machine reach DraftKings?), probe_hosts.py, capture_initial.py
deploy/           user-data.sh (unattended EC2 setup), systemd unit, Caddy config
```

## How I used AI

TODO: rewrite in your own words before submitting.

I captured DraftKings' traffic myself in DevTools (REST, socket frames, a live match) and chose the overall approach. I used Claude to decode the binary socket format from those captures, write most of the code and tests, and analyze the captures. Two findings came out of testing against the real data rather than assumptions: REST can lag the socket, and comparing DraftKings' clock with ours broke update ordering (caught by an integration test, then redesigned).
