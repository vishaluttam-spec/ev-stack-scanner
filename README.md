# EV Stack line scanner

Pulls NFL/CFB lines from The Odds API, diffs soft books (Bovada, BetOnline, LowVig, MyBookie, BetUS, DK, FD, MGM) against Pinnacle, and alerts Telegram on action-grade gaps.

- `data/latest.json` — full line snapshot from the most recent pass
- `data/signals.csv` — every flagged gap, append-only
- Schedule in `.github/workflows/scan.yml` (UTC crons)

Secrets required: `ODDS_API_KEY`, `TELEGRAM_BOT_TOKEN`.
