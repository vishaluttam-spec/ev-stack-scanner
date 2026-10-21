#!/usr/bin/env python3
"""
EV Stack line scanner — runs on GitHub Actions.
Pinnacle = sharp reference. Flags soft-book deviations per Vishal's protocol:
    gap >= 1.5 pts                      -> ACTION
    gap >= 1.0 crossing key number 3/7  -> ACTION*
    gap >= 1.0 otherwise                -> WATCH
Full game every pass; first-half markets only for games kicking off within 150 min.
Writes data/latest.json (full snapshot) and appends data/signals.csv.
Sends Telegram alert for new ACTION flags.
"""
from __future__ import annotations
import csv, json, os, sys, time
from datetime import datetime, timezone, timedelta
import requests

API = "https://api.the-odds-api.com/v4"
SPORTS = {"americanfootball_nfl": "NFL", "americanfootball_ncaaf": "CFB"}
SHARP = "pinnacle"
SOFT = ["bovada", "betonlineag", "lowvig", "mybookieag", "betus", "draftkings", "fanduel", "betmgm"]
ACTION, WATCH = 1.5, 1.0
KEY = {3, 7}
H1_WINDOW_MIN = 150
DATA = "data"
SIGNALS = f"{DATA}/signals.csv"
LATEST = f"{DATA}/latest.json"
ALERTED = f"{DATA}/alerted.json"

ODDS_KEY = os.environ["ODDS_API_KEY"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------- helpers
def get(path, **params):
    r = requests.get(f"{API}{path}", params={"apiKey": ODDS_KEY, **params}, timeout=20)
    r.raise_for_status()
    return r.json(), r.headers.get("x-requests-remaining")


def telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text}, timeout=15)
    except Exception as e:
        print("telegram error:", e)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def full(bookmakers, market, home):
    """{book: {"point": home/over point, "a": home/over price, "b": away/under price}}"""
    out = {}
    for bk in bookmakers:
        for m in bk.get("markets", []):
            if m["key"] != market:
                continue
            d = {}
            for o in m["outcomes"]:
                first = (o["name"] == home) if not market.startswith("totals") else (o["name"] == "Over")
                if first:
                    d["a"] = o.get("price")
                    if "point" in o:
                        d["point"] = float(o["point"])
                else:
                    d["b"] = o.get("price")
            out[bk["key"]] = d
    return out


def implied(am):
    return 100 / (am + 100) if am > 0 else -am / (-am + 100)


def no_vig(a, b):
    if a is None or b is None:
        return None
    pa, pb = implied(a), implied(b)
    return round(100 * pa / (pa + pb), 1)


def extract(bookmakers, market, home):
    """{book: (point, price_home_or_over)}"""
    out = {}
    for bk in bookmakers:
        for m in bk.get("markets", []):
            if m["key"] != market:
                continue
            for o in m["outcomes"]:
                if market.startswith("spreads") and o["name"] == home:
                    out[bk["key"]] = (float(o["point"]), o.get("price"))
                elif market.startswith("totals") and o["name"] == "Over":
                    out[bk["key"]] = (float(o["point"]), o.get("price"))
                elif market == "h2h" and o["name"] == home:
                    out[bk["key"]] = (None, o.get("price"))
    return out


def crosses_key(a, b):
    lo, hi = sorted((abs(a), abs(b)))
    return lo != hi and any(lo <= k <= hi for k in KEY)


def grade(sharp, soft):
    gap = abs(soft - sharp)
    if gap < WATCH:
        return None, gap
    if gap >= ACTION:
        return "ACTION", gap
    if crosses_key(soft, sharp):
        return "ACTION*", gap
    return "WATCH", gap


# ---------------------------------------------------------------- main
def main():
    os.makedirs(DATA, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    alerted = load_json(ALERTED, {})
    snapshot = {"scanned_at_utc": stamp, "games": []}
    signals, alerts, remaining = [], [], None

    for sport, league in SPORTS.items():
        try:
            games, remaining = get(f"/sports/{sport}/odds", regions="us,eu",
                                   markets="h2h,spreads,totals", oddsFormat="american")
        except requests.HTTPError as e:
            print(f"{league}: {e.response.status_code} {e.response.text[:100]}")
            continue
        for g in games:
            home, away = g["home_team"], g["away_team"]
            label = f"{away} @ {home}"
            kick = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
            mins_to_kick = (kick - now).total_seconds() / 60
            if mins_to_kick < -30:          # already well underway
                continue
            bks = g.get("bookmakers", [])
            rec = {"league": league, "game": label, "home": home, "away": away,
                   "kickoff_utc": g["commence_time"], "markets": {}}
            markets = [("FG", "spread", "spreads"), ("FG", "total", "totals")]
            if 0 <= mins_to_kick <= H1_WINDOW_MIN and league == "NFL":
                try:
                    ev, remaining = get(f"/sports/{sport}/events/{g['id']}/odds",
                                        regions="us,eu", markets="spreads_h1,totals_h1",
                                        oddsFormat="american")
                    bks = bks + ev.get("bookmakers", [])
                    markets += [("1H", "spread", "spreads_h1"), ("1H", "total", "totals_h1")]
                except requests.HTTPError:
                    pass
            rec["note"] = "a = home side (or Over) price, b = away side (or Under) price; points are home/over"
            rec["markets"]["ml"] = full(bks, "h2h", home)
            pin = rec["markets"]["ml"].get(SHARP, {})
            rec["pinnacle_fair_home_cents"] = no_vig(pin.get("a"), pin.get("b"))
            for period, mname, key in markets:
                lines = extract(bks, key, home)
                rec["markets"][f"{period}_{mname}"] = full(bks, key, home)
                sharp = lines.get(SHARP)
                if sharp is None:
                    continue
                for book in SOFT:
                    if book not in lines:
                        continue
                    soft = lines[book][0]
                    gr, gap = grade(sharp[0], soft)
                    if not gr:
                        continue
                    row = [stamp, league, label, period, mname, sharp[0], book, soft,
                           round(gap, 1), gr, round(mins_to_kick)]
                    signals.append(row)
                    sid = f"{label}|{period}|{mname}|{book}"
                    if gr.startswith("ACTION") and alerted.get(sid) != soft:
                        alerted[sid] = soft
                        side = "home" if mname == "spread" else "over"
                        alerts.append(
                            f"{gr} {league} {label}\n{period} {mname}: Pinnacle {sharp[0]:+g} "
                            f"vs {book} {soft:+g} (gap {gap:g})\nkick in {round(mins_to_kick)} min")
            snapshot["games"].append(rec)

    # ---- write outputs
    with open(LATEST, "w") as f:
        json.dump(snapshot, f, indent=1)
    new = not os.path.exists(SIGNALS)
    with open(SIGNALS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "league", "game", "period", "market", "pinnacle",
                        "book", "book_line", "gap", "grade", "mins_to_kick"])
        w.writerows(signals)
    with open(ALERTED, "w") as f:
        json.dump(alerted, f)

    print(f"{len(snapshot['games'])} games, {len(signals)} signals, "
          f"{len(alerts)} new alerts, credits remaining {remaining}")
    for s in signals:
        print(" ", s[9], s[1], s[2], s[3], s[4], "pinn", s[5], s[6], s[7])
    if alerts:
        telegram("🚨 EV Stack line alert\n\n" + "\n\n".join(alerts[:8]))
    if remaining and int(remaining) < 1500:
        telegram(f"⚠️ Odds API credits low: {remaining} left this month")


if __name__ == "__main__":
    main()
