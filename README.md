# EV Stack line scanner

Pulls NFL/CFB lines from The Odds API, diffs soft books (Bovada, BetOnline, LowVig, MyBookie, BetUS, DK, FD, MGM) against Pinnacle, and alerts Telegram on action-grade gaps.

- `data/latest.json` — full line snapshot from the most recent pass
- `data/signals.csv` — every flagged gap, append-only
- Schedule in `.github/workflows/scan.yml` (UTC crons)

Secrets required: `ODDS_API_KEY`, `TELEGRAM_BOT_TOKEN`.

## Kalshi add-on (`kalshi.py`)

Each scan also pulls Kalshi's football contracts (NFL + college: game winner, spread ladder, total ladder; NFL 1st-half when Pinnacle 1H lines are in the pass), prices every rung from Pinnacle's no-margin line, and computes the edge of buying YES or NO at the Kalshi ask after the taker fee (≈ 0.07 × P × (1 − P), rounded up to the cent).

- `data/kalshi.json` — every priced contract within 6 pts of Pinnacle's line: fair, asks, fees, edges, size
- `data/kalshi_signals.csv` — append-only log of edges ≥ 2¢
- `latest.json` games carry a `kalshi` list of their best-graded contracts
- Telegram "KALSHI price edge" alert: 🟢 ACTION 5¢+, 🟡 ALERT 4¢, only for rungs within 3 pts of Pinnacle's line, price 10–90¢, 25+ contracts offered; re-alerts only if the edge grows 2¢+
- Model check (Oct 21, 2026): within ~1¢ of Kalshi mid-prices near the line, no directional bias
- Public market data only — no Kalshi account, key, or orders. Price check (d) only; trades still need the full protocol.

## Odds mismatch + "both agree" (added Oct 21, 2026)

- **Odds mismatch:** when a book posts the same number as Pinnacle (spread, total, or who-wins) but its odds beat Pinnacle's no-margin price by 3+ win-% points, it is logged to `data/price_signals.csv` and sent as a "💲 ODDS mismatch" Telegram alert.
- **Both agree:** each game in `latest.json` carries `book_value` (sportsbook point gaps and odds mismatches with their side). When a Kalshi edge points to the same side, the Kalshi alert is tagged "✅ BOTH AGREE", listed first, and `kalshi.json` records it under `confirmed_by`.

## Soccer first-half check (`soccer_1h.py`, added Nov 25, 2026)

Contract: Kalshi soccer "1st Half Total — Over 0.5 goals" (a goal before halftime). Runs hourly (`.github/workflows/soccer.yml`, :20 past each hour UTC).

- Finds every open Kalshi soccer 1H Over 0.5 market; for games kicking off 15–75 min out it matches the game on The Odds API (kickoff ±25 min + both team names) and pulls Pinnacle's `totals_h1` (1 credit per game, `regions=eu`, `bookmakers=pinnacle`).
- Pinnacle's no-margin first-half total (0.5 / 0.75 / 1.0 / 1.25 / 1.5, Asian quarter lines included) is turned into a fair chance of a first-half goal with a Poisson goal model.
- Edge = fair − (Kalshi ask + taker fee), for YES (a goal) and NO (0–0 at HT). Telegram "⚽ SOCCER 1st-half edge": 🟢 ACTION 5¢+, 🟡 ALERT 4¢, 25+ contracts at the ask, re-alerts only if the edge grows 2¢+.
- Paper test (no money): every game's last pre-kick Kalshi price, Pinnacle line/fair and result go to `data/soccer_1h_log.json` / `.csv`. A Monday-morning Telegram scorecard grades the "78% rule" (buy YES at 78¢+) against the "Pinnacle edge rule" (4¢+ after fee) at $10 flat per trade, plus a model check (Pinnacle fair vs actual goal rate).
- Credit guard: stops Pinnacle pulls when the Odds API balance drops below 2,500. Kickoff from Kalshi = 1H market occurrence time − 4h (verified against Bovada/Kalshi app times).
- Model caveat: first halves end 0–0 a little more often than a pure Poisson model says, so fair YES prices can run slightly high; the model-check line in the scorecard measures this.
