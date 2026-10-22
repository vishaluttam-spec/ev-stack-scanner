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
Kalshi add-on (kalshi.py): prices Kalshi contracts vs Pinnacle -> data/kalshi.json,
data/kalshi_signals.csv, and a separate Telegram "KALSHI price edge" alert.
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


BOOK_NAMES = {"pinnacle": "Pinnacle", "bovada": "Bovada", "betonlineag": "BetOnline",
              "lowvig": "LowVig", "mybookieag": "MyBookie", "betus": "BetUS",
              "draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM"}


def bname(key):
    return BOOK_NAMES.get(key, key)


def pt_kick(kick):
    """Kickoff as Pacific time, e.g. 'Sat 12:30 PM PT'. Handles PDT/PST without tz libs."""
    # PDT runs 2nd Sun Mar -> 1st Sun Nov; close enough for football season to check month/day.
    y = kick.year
    mar = datetime(y, 3, 8, 10, tzinfo=timezone.utc)
    mar += timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(y, 11, 1, 9, tzinfo=timezone.utc)
    nov += timedelta(days=(6 - nov.weekday()) % 7)
    off = -7 if mar <= kick < nov else -8
    local = kick + timedelta(hours=off)
    return local.strftime("%a %-I:%M %p") + " PT"


def until(mins):
    if mins < 90:
        return f"in {round(mins)} min"
    h = mins / 60
    return f"in {h:.0f} hrs" if h < 48 else f"in {h / 24:.1f} days"


def fmt_spread(team, pt):
    return f"{team} {pt:+g}" if pt else f"{team} PK"


def describe(mname, home, away, sharp_pt, soft_pt):
    """Plain-English value side at the soft book. Points are home-side (spread) or the total."""
    if mname == "total":
        return f"OVER {soft_pt:g}" if soft_pt < sharp_pt else f"UNDER {soft_pt:g}"
    # spread: home point. Lower (more negative) at soft book = soft book overrates home -> take away.
    if soft_pt < sharp_pt:
        return fmt_spread(away, -soft_pt)
    return fmt_spread(home, soft_pt)


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
            if mins_to_kick <= 0:           # started: books are posting live lines, not comparable
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
                hits, fresh = [], False
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
                    if gr.startswith("ACTION"):
                        hits.append((gap, book, soft))
                        if alerted.get(sid) != soft:
                            alerted[sid] = soft
                            fresh = True
                if fresh:
                    hits.sort(reverse=True)
                    gap, book, soft = hits[0]
                    half = "1st half " if period == "1H" else ""
                    if mname == "spread":
                        sharp_txt = fmt_spread(home, sharp[0])
                        others = ", ".join(f"{bname(b)} {s:+g}" for _, b, s in hits)
                    else:
                        sharp_txt = f"{sharp[0]:g}"
                        others = ", ".join(f"{bname(b)} {s:g}" for _, b, s in hits)
                    early = ("\nEarly — recheck 60–90 min before kick." if mins_to_kick > 24 * 60 else "")
                    alerts.append(
                        f"{league} {label}\n"
                        f"Kick: {pt_kick(kick)} ({until(mins_to_kick)})\n"
                        f"{half}{mname.upper()}: sharp (Pinnacle) {sharp_txt} | {others}\n"
                        f"➡️ Value side: {describe(mname, home, away, sharp[0], soft)} "
                        f"at {bname(book)} ({gap:g}-pt gap)"
                        f"{early}")
            snapshot["games"].append(rec)

    # ---- Kalshi add-on: price Kalshi contracts against Pinnacle (never blocks the line scan)
    kalshi_alerts = []
    try:
        import kalshi
        kalshi_alerts = kalshi.run(snapshot, now, pt_kick, until)
    except Exception as e:
        print("kalshi add-on error:", repr(e))

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
        telegram("🚨 EV Stack line alert\n(signal only — confirm on Kalshi before trading)\n\n"
                 + "\n\n".join(alerts[:8]))
    if kalshi_alerts:
        telegram("💰 EV Stack KALSHI price edge\n"
                 "(price check only — trade only on my TRADE or LEAN message)\n\n"
                 + "\n\n".join(kalshi_alerts[:8]))
    if remaining and int(remaining) < 1500:
        telegram(f"⚠️ Odds API credits low: {remaining} left this month")


if __name__ == "__main__":
    main()
