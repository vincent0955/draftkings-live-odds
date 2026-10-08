# DraftKings NFL Live Odds

- **Live site:** https://54-165-170-102.sslip.io
- **Code:** https://github.com/vincent0955/draftkings-live-odds

## Run it locally

Needs Python 3.10+ and a US connection (DraftKings blocks other countries).

```
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m app            # open http://localhost:8000
```

On Windows use `.venv\Scripts\` instead of `.venv/bin/`.

Not in the US? These run without DraftKings:

```
SOURCE=simulate .venv/bin/python -m app    # fake line moves
SOURCE=replay   .venv/bin/python -m app    # a recorded live game
.venv/bin/python -m pytest                 # tests
```

## How it works

1. The server connects to the same WebSocket that DraftKings' own site uses. It only sends what changed, not the full picture.
2. At startup the server loads the full board once from DraftKings' API, then applies the changes on top.
3. Each change is pushed to every open page right away, using Server-Sent Events.
4. Every 60 seconds the server re-checks the API to catch anything it missed.

DraftKings sends separate lists of games, markets and selections. The server maps them into game, market, side, line and odds. A line move arrives as a deleted selection plus a new one, so prices are tracked by market and side, not by DraftKings' IDs.

If the WebSocket drops, the server reconnects and checks the API every 5 seconds until it's back. Bad data is skipped instead of crashing the app.

## Why this approach

- **WebSocket instead of polling.** Polling is always up to one interval behind. The WebSocket gets changes within milliseconds.
- **The server talks to DraftKings, not the browser.** Viewers never contact DraftKings, so they don't need to be in the US.
- **AWS instead of a free host.** The server keeps a connection open all the time, which free serverless hosts like Vercel don't allow.
- **Getting past DraftKings' blocks.** DraftKings blocks visitors outside the US, so the server runs in the US. Its bot protection (Akamai) also blocks cloud servers from the API, so that one call a minute goes through a US proxy. The WebSocket isn't blocked and connects directly, so the proxy doesn't slow down updates. No login, cookie or token is needed (the WebSocket's token is the fixed string `default-token`), so nothing can expire. I didn't hit any rate limits. The app makes one WebSocket connection and one API call a minute, about the same as one person with the page open.

## How fresh the odds are

- **About 40 ms** from DraftKings publishing a change to our server receiving it, measured on AWS. This is from the first line moves only, since no NFL game was live before the deadline.
- **Plus your own network delay** from our server to your browser.

The top of the page shows both numbers live, measured on every line move. "checked 3s ago" in the header means the connection to DraftKings was confirmed 3 seconds ago (the server pings it every 5 seconds). If that stops for 30 seconds, the page warns that the odds may be out of date.

## Adding a second sportsbook or league

- **Another league** is mostly config. The league and category IDs are settings (`DK_LEAGUE_ID`, `DK_SUBCATEGORY_ID`), and DraftKings labels moneyline, spread and total the same way in every sport.
- **Another sportsbook** is one new adapter that turns that book's feed into the same changes (game, market, side, line, odds). The rest of the app stays the same. The hard part is matching the same game across books ("LA Rams" vs "Los Angeles Rams").
- **Where AI helps:** drafting a new adapter from recorded traffic (the way this one was built), building the team and market name mapping, and flagging when a book changes its format (a jump in rejected rows).
