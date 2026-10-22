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
