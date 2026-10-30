---
name: trader-autopilot
description: Runs the whole pre-trade chain on a schedule so you only show up for the decision. Morning it refreshes the regime flag, the daily scan and the news, ranks the top picks by Jev quality, then watches each pick's opening-range breakout bar by bar until one triggers or the no-entries-after cutoff. On a trigger it runs the exit plan, the Time Machine, sizing, the risk check and the ticket, and writes trader/decision.json for the dashboard plus a Mac notification. It never places an order; your yes still happens in chat and on the IBKR review page.
---

# trader-autopilot

Not financial advice. This skill never sends an order. It stops at the ticket and
waits for your yes, which still goes through chat and the broker's review page.

## What it does

```bash
python3 day-trader-skill/trader-autopilot/autopilot.py day       # morning, then watch, then the card
python3 day-trader-skill/trader-autopilot/autopilot.py morning   # regime, daily scan, news -> ranked picks
python3 day-trader-skill/trader-autopilot/autopilot.py watch     # poll the saved picks until a trigger
```

### Morning

Three steps, in order, each one a child process with a timeout. A failed step is
recorded and the day continues:

1. `regime.py` with no `--bars`, so it uses the Yahoo fallback. The IBKR bars need
   Claude, and nothing here can reach the connector.
2. `scan.py daily --quiet` over the whole universe. This is the slow one.
3. `news.py --json --hours 24` for the watchlist, plan and portfolio tickers.

The result goes to `trader/state/autopilot.json`:

```json
{"date": "2026-10-30",
 "steps": [{"name": "regime", "ok": true, "rc": 0, "seconds": 3.1, "tail": "..."}],
 "picks": [{"symbol": "TSLA", "quality": 3.7, "setups": ["orb"]}]}
```

`picks` are the top five hits of `trader/watchlist.json` by `jev.quality`.

### Watch

Every 60 seconds, for each pick in rank order, it runs
`entry.py check SYMBOL --setup orb --json`. The first pick that reports
`triggered` wins; any others that fired in the same poll are listed as
`also_triggered`. The winner is checked once more with `--write`, so
`trader/entry.json` exists for the chain. The loop stops, with no card, at
`no_entries_after` from `trader/config.json` (default 15:30 New York).

### The card

On a trigger it runs, in order:

```bash
python3 day-trader-skill/trader-exit/exit.py plan --stop or-mid --target 2R --write
python3 day-trader-skill/trader-timemachine/timemachine.py run --write
python3 day-trader-skill/trader-budget/size.py --write
python3 day-trader-skill/trader-risk/check.py --json
python3 day-trader-skill/trader-broker/ticket.py show
```

and writes `trader/decision.json`. Its `status` is one of:

| status | meaning |
|---|---|
| `ready` | every step passed, risk check PASS, account snapshot fresh |
| `blocked` | the risk check said BLOCK on at least one order |
| `stale_account` | `trader/portfolio.json` is older than 24 hours; sync in Claude first |
| `error` | a step failed; read its `tail` |

The dashboard renders the card at the top of the trade panel. A Mac notification
says the symbol and the status. The `reply_hint` on the card is the exact line to
type in chat, for example `yes buy 8 TSLA`.

## What it never does

- Place, modify or cancel an order.
- Call `ticket.py approve`.
- Sync the account or fetch IBKR bars. Those need the connector inside Claude.
- Retry Yahoo. The scripts it calls already pace themselves.

## Schedule

`~/Library/LaunchAgents/com.daytrader.autopilot.plist` starts `autopilot.py day`
at 9:00 AM New York on weekdays. The plist passes no working directory, because
launchd cannot enter the external volume; the script changes into
`/Volumes/Extreme SSD/Day trader Setup` itself. Log:
`~/Library/Logs/daytrader-autopilot.log`. On a weekend the script prints one line
and exits 0.

## Offline and replay

Every child script keeps its own `--offline` and `--now` flags. The autopilot
takes `--root` for a different project folder and `--every` for the poll
interval; the tests inject a fake runner instead of `subprocess.run`.
