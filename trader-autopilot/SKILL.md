---
name: trader-autopilot
description: Runs the morning steps on a schedule, watches the top scan picks, and writes a decision card when one triggers. Never places an order.
---

# trader-autopilot

`autopilot.py day` = `morning` (regime, daily scan, news, ranked picks into
`trader/state/autopilot.json`) then `watch` (poll each pick's ORB setup until
one triggers or `no_entries_after`). On a trigger it runs exit plan, time
machine, size, risk check and `ticket.py show`, and writes `trader/decision.json`
for the dashboard plus a Mac notification. The yes still happens in chat and on
the IBKR review page. Scheduled by `~/Library/LaunchAgents/com.daytrader.autopilot.plist`.
