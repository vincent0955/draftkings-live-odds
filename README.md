# DraftKings NFL Live Odds

A page showing DraftKings' NFL main lines (spread, total, moneyline) that updates by itself within a fraction of a second of DraftKings moving a line.

- Live site: https://54-165-170-102.sslip.io
- Raw status and latency: https://54-165-170-102.sslip.io/health

## Run it locally

Needs Python 3.10+.

```
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt    # Windows: .venv\Scripts\pip
.venv/bin/python -m app                           # open http://localhost:8000
```

Live mode (the default) needs a US connection, because DraftKings blocks other countries. These work from anywhere:

```
SOURCE=replay   python -m app    # replays a real recorded live match
SOURCE=simulate python -m app    # fake NFL line moves in DraftKings' real format
python -m pytest                 # 34 tests
```

In PowerShell, set variables with `$env:SOURCE="replay"`. If DraftKings blocks your IP (it blocks AWS), set `DK_HTTP_PROXY` to a US proxy.

## How it works

```
DraftKings WebSocket (changes only) ──> server (odds in memory) ──SSE──> browsers
DraftKings REST API (full board) ─────> server, at startup and every 60 s
```

1. The server subscribes to DraftKings' WebSocket for NFL game lines. It's the same feed DraftKings' own site uses.
2. The socket only sends changes, so the server loads the full board once from DraftKings' REST API. It subscribes first and loads second, so no change can slip through the gap.
3. Each socket message updates the affected prices, and the server pushes them to every open page.
4. Every 60 seconds it re-checks REST and fixes anything the socket missed.

## Why this approach

- **Socket instead of polling.** The socket delivers a change within milliseconds. Polling is always up to one interval behind, and DraftKings caches REST for 1 second anyway.
- **One server talks to DraftKings, not each viewer.** Viewers never hit DraftKings, so its geo block doesn't affect them, and DraftKings sees one client no matter how many people have the page open.
- **Server-Sent Events to the browser.** Data only goes one way, and SSE reconnects by itself. No frontend framework needed.
- **EC2 instead of Vercel.** The server holds a connection open all day, which serverless functions can't do. It's one t3.small in us-east-1 with Caddy for HTTPS.

## Freshness and latency

The top of the page shows latency, measured on every line move:

- **DraftKings to our server:** from DraftKings' publish timestamp to when the server receives it. DraftKings' clock differs from ours (by 0.4 to 1.7 s on my laptop), so the server estimates the offset from the subscribe round trip and corrects for it.
- **Our server to browser:** each browser syncs its clock with the server and times every push.

Measured on a recorded live match:

| | median | slowest 5% |
|---|---|---|
| DraftKings' own processing (created to published) | 23 ms | 76 ms |
| DraftKings to my laptop in Japan, through a US VPN | 128 ms | 152 ms |

I don't have AWS numbers yet. No NFL game ran between the deploy and the deadline, and pregame lines barely moved. The socket round trip from AWS to DraftKings is 19 to 36 ms, so it should beat the laptop by a lot. Thursday Night Football (TB @ DAL) is the first live NFL game, and the page will show the real numbers from then on.

**Staleness:** "checked 3s ago" in the header means the feed was confirmed alive 3 seconds ago, not that a line moved then. A line can sit still for hours and still be current. The server pings DraftKings every 5 seconds. If confirmation stops for 30 seconds, or the page loses the server, a warning appears saying the odds may be out of date.

## Auth, cookies, geo and bot protection

- **Auth and cookies:** none needed. The socket's token is literally the string `default-token`.
- **Geo:** DraftKings blocks non-US visitors. I'm in Japan, so I used a US VPN (Windscribe) to record traffic and test. DraftKings picks a state from your IP and puts it in every URL (`US-IL-SB`, `dkusil`). This app uses Illinois (`DK_STATE`).
- **Bot protection:** Akamai returns 403 to AWS for the REST API (IPv4 and IPv6, every state I tried). The socket isn't blocked. So the server sends only its REST calls through a US residential proxy: one call a minute, about 12 KB each. The socket still connects directly, so the proxy adds no latency. The proxy credentials live only on the server.
- **What I tried first:** letting each viewer's browser load the board from DraftKings (the API allows cross-origin requests). It worked in the US but failed for anyone outside it, so now it's only the fallback if the proxy fails.
- **Rate limits:** one socket plus one REST call a minute, the same as one person with the page open.

## Data shape

**REST:** `sportsbook-nash.draftkings.com/sites/US-IL-SB/api/sportscontent/controldata/league/leagueSubcategory/v1/markets`, with league `88808` (NFL) and subcategory `4518` (Game Lines). It returns three flat lists, `events`, `markets` and `selections`, joined by ID.

**Socket:** `wss://sportsbook-ws-us-il.draftkings.com/websocket?format=msgpack`. Binary MessagePack. Each update has inserts, deletes and changes, sent as arrays with no field names, just a type number (24 = price change, 35 = new selection, 23 = market change). I worked out the field positions from about 700 recorded messages.

Two things I only found from the recordings:

- **A line move is a new selection.** The line is part of the selection ID: `0HC84695450N300_1` is the home side at -3. When it moved to -2.5, DraftKings deleted that ID and created `0HC84695450N250_1`. So the app tracks prices by (market, side), not by ID, and can show "was -3".
- **REST can lag the socket.** In one recording REST kept returning an old price for several seconds after the socket had the new one. So the 60-second check only overrides a price if REST disagrees twice in a row with no socket update in between.

What the app serves at `/api/odds`, per game (some fields trimmed):

```json
{"name": "TB Buccaneers @ DAL Cowboys", "away": "TB Buccaneers", "home": "DAL Cowboys",
 "start": 1791504900, "status": "NOT_STARTED",
 "markets": {
   "spread": {"suspended": false, "outcomes": {
     "away": {"line": 8.5,  "american": -108, "available": true, "prev_american": null, "changed_at": null},
     "home": {"line": -8.5, "american": -112, "available": true, "prev_american": null, "changed_at": null}}},
   "total": {...}, "moneyline": {...}}}
```

## When things go wrong

- **Socket drops:** reconnects with backoff (1 s, 2 s, 4 s, up to 30 s), polling REST every 5 seconds meanwhile.
- **Connection silently dies:** the 5-second ping catches it.
- **DraftKings unreachable:** the page keeps the last good odds and warns that they may be out of date.
- **Bad or unexpected data:** every row is checked on its own. Bad rows are dropped and counted, never fatal. An unknown message type triggers a fresh REST load.
- **Out-of-order messages:** dropped using DraftKings' timestamps.
- **Browser loses the server:** it reconnects by itself and gets the full board again.

`tests/test_feed_integration.py` runs these cases against a fake DraftKings server.

## Testing without a live NFL game

There wasn't one before the deadline, so:

- Tests run on real recordings: NFL boards from two days, 717 messages from a live tennis match, and baseball games ending.
- `SOURCE=replay` plays the tennis match through the real pipeline. `SOURCE=simulate` makes NFL moves in DraftKings' exact format.
- I ran it live on my laptop against tennis: 114 updates in 6.5 minutes, all applied, none dropped. On the NFL board it matched DraftKings' site line for line.

## Bonus items

- **Line moves:** prices flash green or red when they go up or down, yellow when the line moves, and show "was ..." for 2 minutes.
- **Latency and staleness:** top of the page, plus `/health`.
- **Second league:** mostly config (`DK_LEAGUE_ID`, `DK_SUBCATEGORY_ID`). Market types come from DraftKings' `betOfferTypeId`, which is the same across sports. The tennis replay already runs through the same code.
- **Second sportsbook:** not built. I'd add one adapter per book that outputs the same (game, market, side, line, odds) changes. The hard part is matching games and teams across books ("LA Rams" vs "Los Angeles Rams").

## Code layout

```
app/protocol.py   socket protocol: subscribe, decode
app/snapshot.py   REST fetch and parsing
app/book.py       odds in memory: apply board and changes
app/feed.py       live loop: subscribe, load, reconnect, re-check
app/server.py     web server: page, /api/odds, /api/stream, /health
static/index.html the page
fixtures/         real DraftKings recordings used by tests and replay
scripts/probe.py  checks whether a machine can reach DraftKings
deploy/           EC2 setup, systemd service, Caddy, proxy setup
```

## How I used AI

I built this with Claude (Anthropic's AI) writing most of the code. The parts it couldn't do were mine:

- **Getting the data.** DraftKings blocks Japan, and Claude isn't allowed to open DraftKings in a browser, so it never saw the site. I set up a US VPN, recorded DraftKings' traffic in Chrome DevTools, and found a live game to record (a tennis match, since no NFL game was on). Everything the app knows about DraftKings' format came from those recordings.
- **Checking it against the real thing.** I ran the app on my laptop over the VPN and compared it with DraftKings' site.
- **Design questions.** Early on I flagged that this had to run in the cloud without my laptop, so we checked whether a server could reach DraftKings before building the deploy (that's `scripts/probe.py`). I asked how we'd test with no NFL games, which led to the replay and simulate modes. When the browser-loads-the-board workaround came up, I questioned why viewers' browsers should fetch anything if the server is on AWS, and offered a proxy I already had. That's what runs now.
- **Accounts and secrets.** I set up AWS, GitHub, the VPN and the proxy, and entered the proxy credentials on the server myself.

Claude decoded the socket format from my recordings, wrote most of the code, tests and deploy scripts, and drafted this README.
