#!/usr/bin/env python3
"""
Kalshi add-on for the EV Stack line scanner.

For every game in the scanner snapshot it finds the matching Kalshi contracts
(game winner, spread ladder, total ladder; NFL first half when Pinnacle 1H
lines are in the snapshot), prices each contract from Pinnacle's no-margin
line, and computes the edge of buying YES or NO at the current Kalshi ask
after Kalshi's taker fee:

    edge = fair - (ask + fee)          fee = ceil(0.07 * P * (1 - P)) per contract

Grades (Vishal's EV Stack thresholds):  5c+ ACTION, 4c ALERT, 1-3c logged only.
This is check (d) only. Trades still need the public/sharp/football checks.

Writes data/kalshi.json (all priced contracts near the line), appends
data/kalshi_signals.csv (edges >= 2c), and returns Telegram alert text.
Public market data only: no Kalshi account, key or orders.
"""
from __future__ import annotations
import csv, json, math, os, re, time, unicodedata
from datetime import datetime, timezone

import requests

API = "https://api.elections.kalshi.com/trade-api/v2"
DATA = "data"
OUT = f"{DATA}/kalshi.json"
SIGNALS = f"{DATA}/kalshi_signals.csv"
ALERTED = f"{DATA}/kalshi_alerted.json"

ACTION_C, ALERT_C, LOG_C = 5, 4, 2        # cents
NEAR_LINE = 3.0        # alert only on ladder rungs within 3 pts of Pinnacle's line
STORE_LINE = 6.0       # keep rungs within 6 pts in kalshi.json
MIN_PRICE, MAX_PRICE = 0.10, 0.90   # skip longshots / near-certainties (model error dominates)
MIN_SIZE = 25          # contracts offered at the ask
MAX_DAYS = 8           # ignore games further out
REALERT_C = 2          # re-alert a contract only if its edge grows by 2c+

# Kalshi series per league: (series, kind, period)
SERIES = {
    "NFL": [("KXNFLGAME", "winner", "FG"), ("KXNFLSPREAD", "spread", "FG"),
            ("KXNFLTOTAL", "total", "FG"), ("KXNFL1HSPREAD", "spread", "1H"),
            ("KXNFL1HTOTAL", "total", "1H")],
    "CFB": [("KXNCAAFGAME", "winner", "FG"), ("KXNCAAFSPREAD", "spread", "FG"),
            ("KXNCAAFTOTAL", "total", "FG")],
}

# Outcome spread (standard deviation, points) used to price ladder rungs off the sharp line.
SIGMA = {("NFL", "FG", "spread"): 13.5, ("NFL", "FG", "total"): 13.5,
         ("NFL", "1H", "spread"): 9.5, ("NFL", "1H", "total"): 9.5,
         ("CFB", "FG", "spread"): 16.5, ("CFB", "FG", "total"): 17.0}

# Key-number weights on final margin (|margin| -> multiplier on a normal curve).
# Approximates how often NFL games land on 3, 7, 10, 14 etc. Untuned values default to 0.85.
NFL_KEYS = {0: 0.06, 1: 0.70, 2: 0.75, 3: 2.50, 4: 0.95, 5: 0.60, 6: 1.20, 7: 1.70,
            8: 0.80, 9: 0.60, 10: 1.25, 11: 0.65, 12: 0.65, 13: 0.80, 14: 1.25,
            15: 0.60, 16: 0.75, 17: 1.25, 18: 0.75, 19: 0.60, 20: 0.90, 21: 1.20}
NFL_1H_KEYS = {0: 2.40, 1: 0.60, 2: 0.60, 3: 2.00, 4: 0.90, 5: 0.60, 6: 1.00, 7: 1.60,
               8: 0.70, 9: 0.60, 10: 1.30, 11: 0.70, 12: 0.60, 13: 0.70, 14: 1.30}
CFB_KEYS = {0: 0.03, 3: 1.60, 7: 1.40, 10: 1.10, 14: 1.10, 17: 1.05, 21: 1.05}


# ------------------------------------------------------------------ http
_last = [0.0]


def kget(path, **params):
    """Paced GET with backoff on 429 (Kalshi throttles bursts)."""
    for attempt in range(6):
        wait = 0.35 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        r = requests.get(f"{API}{path}", params=params, timeout=20)
        if r.status_code == 429:
            time.sleep(min(2 ** attempt, 20))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Kalshi rate limit persisted on {path}")


def fetch_events(series):
    out, cursor = [], None
    for _ in range(10):
        p = {"series_ticker": series, "status": "open", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            p["cursor"] = cursor
        d = kget("/events", **p)
        out += d.get("events", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


# ------------------------------------------------------------------ names
ALIAS = {"southern miss": "southern mississippi", "ul monroe": "louisiana monroe",
         "ulm": "louisiana monroe", "hawai'i": "hawaii", "app state": "appalachian state",
         "miami (fl)": "miami", "uconn": "uconn", "umass": "umass", "ole miss": "ole miss",
         "fiu": "florida international", "fau": "florida atlantic", "utep": "utep",
         "utsa": "utsa", "unlv": "unlv", "usf": "south florida", "ucf": "ucf"}
QUALIFIERS = {"state", "tech", "a&m", "christian", "southern", "international", "st",
              "atlantic", "(oh)", "carolina", "kentucky", "illinois", "michigan", "texas",
              "florida", "georgia", "mississippi", "dakota"}


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()
    s = re.sub(r"\bst\.$", "state", s)
    s = re.sub(r"\bst\.\s", "state ", s)
    s = re.sub(r"\s+", " ", s.replace("-", " "))
    for k, v in ALIAS.items():          # prefix aliases, applied to both Kalshi and Odds API names
        if s == k or s.startswith(k + " "):
            s = v + s[len(k):]
            break
    return s


def name_match(kalshi_name, full_name):
    """Kalshi short name ('Michigan St.', 'New York J') vs Odds API full name."""
    k, f = norm(kalshi_name), norm(full_name)
    if f == k:
        return True
    if not f.startswith(k):
        return False
    rest = f[len(k):]
    if not rest:
        return True
    if k[-1].isalpha() and len(k.split()[-1]) == 1:    # 'new york j' -> 'new york jets'
        return True
    if rest[0] != " ":
        return False
    nxt = rest.split()[0]
    return nxt not in QUALIFIERS      # 'texas' must not match 'texas tech red raiders'


def team_side(label, home, away):
    """Which game team a Kalshi label refers to: 'home', 'away' or None."""
    h, a = name_match(label, home), name_match(label, away)
    if h and not a:
        return "home"
    if a and not h:
        return "away"
    if h and a:       # both prefix-match (e.g. Los Angeles R / C): prefer longer exact token
        return "home" if len(norm(home)) <= len(norm(away)) else "away"
    return None


# ------------------------------------------------------------------ fair value model
def _pmf(mu, sigma, keys, lo, hi):
    xs = range(lo, hi + 1)
    w = []
    for x in xs:
        d = math.exp(-0.5 * ((x - mu) / sigma) ** 2)
        if keys is not None:
            d *= keys.get(abs(x), 0.85)
        w.append(d)
    t = sum(w)
    return {x: v / t for x, v in zip(xs, w)}


def implied(am):
    return 100 / (am + 100) if am > 0 else -am / (-am + 100)


def no_vig(a, b):
    if a is None or b is None:
        return None
    pa, pb = implied(a), implied(b)
    return pa / (pa + pb)


def _cover(pmf, line):
    """P(outcome > line), excluding a push when line is whole."""
    over = sum(p for x, p in pmf.items() if x > line)
    push = pmf.get(line, 0.0) if float(line).is_integer() else 0.0
    return over / (1 - push) if push < 1 else 0.5


def calibrate(kind, line, p_over, sigma, keys, lo, hi):
    """Find the centre of the outcome distribution so the model reproduces
    Pinnacle's no-margin probability at Pinnacle's own line."""
    a, b = lo + 5, hi - 5
    for _ in range(50):
        m = (a + b) / 2
        if _cover(_pmf(m, sigma, keys, lo, hi), line) < p_over:
            a = m
        else:
            b = m
    return _pmf((a + b) / 2, sigma, keys, lo, hi)


def margin_model(league, period, pin_spread):
    """pin_spread = {'point': home handicap, 'a': home price, 'b': away price}."""
    if not pin_spread or pin_spread.get("point") is None:
        return None, None
    p_home = no_vig(pin_spread.get("a"), pin_spread.get("b"))
    if p_home is None:
        return None, None
    keys = (NFL_KEYS if period == "FG" else NFL_1H_KEYS) if league == "NFL" else CFB_KEYS
    sigma = SIGMA[(league, period, "spread")]
    L = pin_spread["point"]
    # home covers when home margin > -L
    pmf = calibrate("spread", -L, p_home, sigma, keys, -80, 80)
    return pmf, -L


def total_model(league, period, pin_total):
    if not pin_total or pin_total.get("point") is None:
        return None, None
    p_over = no_vig(pin_total.get("a"), pin_total.get("b"))
    if p_over is None:
        return None, None
    sigma = SIGMA[(league, period, "total")]
    pmf = calibrate("total", pin_total["point"], p_over, sigma, None, 0, 160)
    return pmf, pin_total["point"]


def p_gt(pmf, k):
    return sum(p for x, p in pmf.items() if x > k)


def p_lt(pmf, k):
    return sum(p for x, p in pmf.items() if x < k)


# ------------------------------------------------------------------ pricing helpers
def fee(p):
    """Kalshi taker fee per contract, rounded up to the cent."""
    return math.ceil(round(7 * p * (1 - p), 6)) / 100


def dollars(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def price_market(m, fair_yes):
    """Edge of buying YES and NO at the current asks. Returns dict or None."""
    ya, na = dollars(m.get("yes_ask_dollars")), dollars(m.get("no_ask_dollars"))
    out = {"ticker": m["ticker"], "contract": m.get("yes_sub_title") or m.get("title"),
           "fair_yes": round(fair_yes, 3), "yes_ask": ya, "no_ask": na,
           "yes_ask_size": num(m.get("yes_ask_size_fp")),
           "no_ask_size": num(m.get("yes_bid_size_fp"))}      # NO ask = 1 - YES bid
    best = None
    for side, ask, fair, size in (("YES", ya, fair_yes, out["yes_ask_size"]),
                                  ("NO", na, 1 - fair_yes, out["no_ask_size"])):
        if ask is None or ask <= 0 or ask >= 1:
            continue
        f = fee(ask)
        edge = round((fair - ask - f) * 100, 1)
        out[f"edge_{side.lower()}_c"] = edge
        out[f"fee_{side.lower()}"] = f
        if best is None or edge > best[1]:
            best = (side, edge, ask, f, size, fair)
    if best is None:
        return None
    out.update(best_side=best[0], best_edge_c=best[1], best_ask=best[2], best_fee=best[3],
               best_size=best[4], best_fair=round(best[5], 3))
    return out


def grade(edge_c):
    if edge_c >= ACTION_C:
        return "ACTION"
    if edge_c >= ALERT_C:
        return "ALERT"
    if edge_c >= 1:
        return "LOG"
    return None


# ------------------------------------------------------------------ matching
def game_suffix(event_ticker):
    return event_ticker.split("-", 1)[1] if "-" in event_ticker else event_ticker


def match_games(snapshot_games, winner_events, now):
    """Map scanner game index -> Kalshi game suffix (e.g. '26OCT22NECHI')."""
    out = {}
    for i, g in enumerate(snapshot_games):
        kick = datetime.fromisoformat(g["kickoff_utc"].replace("Z", "+00:00"))
        best = None
        for ev in winner_events.get(g["league"], []):
            labels = [m.get("yes_sub_title", "") for m in ev.get("markets", [])]
            sides = {team_side(l, g["home"], g["away"]) for l in labels}
            if sides != {"home", "away"}:
                continue
            # date in the ticker (e.g. 26OCT22) must be within 2 days of kickoff
            mt = re.match(r"(\d\d)([A-Z]{3})(\d\d)", game_suffix(ev["event_ticker"]))
            if mt:
                try:
                    d = datetime.strptime(mt.group(0), "%y%b%d").replace(tzinfo=timezone.utc)
                    if abs((d - kick).total_seconds()) > 2.5 * 86400:
                        continue
                except ValueError:
                    pass
            best = game_suffix(ev["event_ticker"])
            break
        if best:
            out[i] = best
    return out


# ------------------------------------------------------------------ text
def cents(x):
    return f"{round(x * 100):d}¢"


def means(p, home, away):
    """Plain-English 'you win if ...' for the side being bought."""
    k, s, side = p["kind"], p.get("strike"), p["best_side"]
    half = " in the 1st half" if p["period"] == "1H" else ""
    if k == "winner":
        team = home if p["team_side"] == "home" else away
        other = away if p["team_side"] == "home" else home
        return f"you win if {team} wins" if side == "YES" else f"you win if {other} wins"
    if k == "total":
        n = int(s + 0.5)
        return (f"you win if {n}+ total points are scored{half}" if side == "YES"
                else f"you win if {n - 1} or fewer total points are scored{half}")
    team = home if p["team_side"] == "home" else away
    other = away if p["team_side"] == "home" else home
    n = int(s + 0.5)
    if side == "YES":
        return f"you win if {team} wins by {n}+ points{half}"
    return f"you win if {other} wins or loses by {n - 1} or fewer{half}"


# ------------------------------------------------------------------ main entry
def run(snapshot, now, pt_kick, until):
    """Adds Kalshi pricing to the snapshot. Returns list of Telegram alert blocks."""
    leagues = {g["league"] for g in snapshot["games"]}
    events = {}          # (league, kind, period) -> {suffix: event}
    winner_events = {}
    for lg in leagues:
        for series, kind, period in SERIES.get(lg, []):
            try:
                evs = fetch_events(series)
            except Exception as e:
                print(f"kalshi {series}: {e}")
                evs = []
            events[(lg, kind, period)] = {game_suffix(e["event_ticker"]): e for e in evs}
            if kind == "winner":
                winner_events[lg] = evs

    games = snapshot["games"]
    matched = match_games(games, winner_events, now)
    alerted = {}
    try:
        with open(ALERTED) as f:
            alerted = json.load(f)
    except Exception:
        pass

    out = {"scanned_at_utc": snapshot["scanned_at_utc"], "games": [], "unmatched": []}
    sig_rows, alerts = [], []
    stamp = snapshot["scanned_at_utc"]

    for i, g in enumerate(games):
        kick = datetime.fromisoformat(g["kickoff_utc"].replace("Z", "+00:00"))
        mins = (kick - now).total_seconds() / 60
        if mins <= 0 or mins > MAX_DAYS * 1440:
            continue
        suffix = matched.get(i)
        if not suffix:
            out["unmatched"].append(g["game"])
            continue
        lg, home, away = g["league"], g["home"], g["away"]
        mk = g["markets"]
        priced = []

        # ---- models from Pinnacle
        models = {}
        for period in ("FG", "1H"):
            pin_sp = mk.get(f"{period}_spread", {}).get("pinnacle")
            pin_to = mk.get(f"{period}_total", {}).get("pinnacle")
            if (lg, period, "spread") in SIGMA:
                models[(period, "spread")] = margin_model(lg, period, pin_sp)
            if (lg, period, "total") in SIGMA:
                models[(period, "total")] = total_model(lg, period, pin_to)
        fair_home = g.get("pinnacle_fair_home_cents")

        for (lg2, kind, period), evmap in events.items():
            if lg2 != lg or suffix not in evmap:
                continue
            ev = evmap[suffix]
            for m in ev.get("markets", []):
                label = m.get("yes_sub_title") or ""
                strike = m.get("floor_strike")
                dist = None
                if kind == "winner":
                    if fair_home is None:
                        continue
                    side = team_side(label, home, away)
                    if side is None:
                        continue
                    fy = fair_home / 100 if side == "home" else 1 - fair_home / 100
                    dist = None
                elif kind == "spread":
                    pmf, home_line = models.get((period, "spread"), (None, None))
                    if pmf is None or strike is None:
                        continue
                    team = re.sub(r"\s+wins by.*$", "", label)
                    side = team_side(team, home, away)
                    if side is None:
                        continue
                    if side == "home":
                        fy = p_gt(pmf, strike)
                        dist = abs(strike - home_line)        # home_line = -pinnacle home point
                    else:
                        fy = p_lt(pmf, -strike)
                        dist = abs(-strike - home_line)
                else:
                    side = None
                    pmf, tline = models.get((period, "total"), (None, None))
                    if pmf is None or strike is None:
                        continue
                    fy = p_gt(pmf, strike)
                    dist = abs(strike - tline)
                if dist is not None and dist > STORE_LINE:
                    continue
                p = price_market(m, fy)
                if not p:
                    continue
                p.update(kind=kind, period=period, strike=strike,
                         team_side=side if kind != "total" else None,
                         pts_from_pinnacle=None if dist is None else round(dist, 1))
                if kind == "winner":
                    p["contract"] = m.get("title") or f"{label} wins"
                priced.append(p)

                # ---- does a sportsbook gap point the same way? ("both agree")
                if kind == "total":
                    kdir = "over" if p["best_side"] == "YES" else "under"
                else:
                    kdir = side if p["best_side"] == "YES" else ("away" if side == "home" else "home")
                p["direction"] = kdir
                p["confirmed_by"] = [b["text"] for b in g.get("book_value", [])
                                     if b["period"] == period and b["side"] == kdir
                                     and (b["market"] == kind
                                          or (kind == "winner" and b["market"] == "spread"))][:3]
                gr = grade(p["best_edge_c"])
                near = dist is None or dist <= NEAR_LINE
                tradeable = (MIN_PRICE <= p["best_ask"] <= MAX_PRICE
                             and p["best_size"] >= MIN_SIZE)
                p["grade"] = gr if (near and tradeable) else (
                    "LOG" if gr else None)
                if gr and p["best_edge_c"] >= LOG_C:
                    sig_rows.append([stamp, lg, g["game"], period, kind, p["ticker"],
                                     p["contract"], p["best_side"], p["best_ask"],
                                     p["best_fee"], p["best_fair"], p["best_edge_c"],
                                     int(p["best_size"]), p["pts_from_pinnacle"], round(mins)])
                if p["grade"] in ("ACTION", "ALERT"):
                    key = f'{p["ticker"]}|{p["best_side"]}'
                    last = alerted.get(key)
                    if last is None or p["best_edge_c"] >= last + REALERT_C:
                        alerted[key] = p["best_edge_c"]
                        alerts.append((p["best_edge_c"], lg, g, kick, mins, p))

        priced.sort(key=lambda x: -x["best_edge_c"])
        out["games"].append({"league": lg, "game": g["game"], "home": home, "away": away,
                             "kickoff_utc": g["kickoff_utc"], "kalshi_game": suffix,
                             "markets": priced})
        g["kalshi"] = [x for x in priced if x.get("grade")][:12]

    # ---- write
    os.makedirs(DATA, exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    new = not os.path.exists(SIGNALS)
    with open(SIGNALS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "league", "game", "period", "market", "ticker", "contract",
                        "side", "ask", "fee", "fair", "edge_c", "size", "pts_from_pinnacle",
                        "mins_to_kick"])
        w.writerows(sig_rows)
    with open(ALERTED, "w") as f:
        json.dump(alerted, f)

    n_priced = sum(len(x["markets"]) for x in out["games"])
    print(f"kalshi: {len(out['games'])} games matched, {len(out['unmatched'])} unmatched, "
          f"{n_priced} contracts priced, {len(sig_rows)} edges >= {LOG_C}c, {len(alerts)} new alerts")
    if out["unmatched"]:
        print("  unmatched:", "; ".join(out["unmatched"][:15]))

    # ---- alert text
    alerts.sort(key=lambda a: (not a[5].get("confirmed_by"), -a[0]))
    blocks = []
    for edge, lg, g, kick, mins, p in alerts:
        half = "1st half " if p["period"] == "1H" else ""
        what = {"winner": "who wins", "spread": f"{half}spread", "total": f"{half}total"}[p["kind"]]
        early = "\nEarly — recheck 60–90 min before kick." if mins > 24 * 60 else ""
        blocks.append(
            f'{"🟢 ACTION" if edge >= ACTION_C else "🟡 ALERT"}'
            f'{" ✅ BOTH AGREE" if p.get("confirmed_by") else ""} {lg} {g["game"]}\n'
            f"Kick: {pt_kick(kick)} ({until(mins)}) · {what}\n"
            f"BUY {p['best_side']} on \"{p['contract']}\" at {cents(p['best_ask'])} or less\n"
            f"({means(p, g['home'], g['away'])})\n"
            f"Fair {p['best_fair'] * 100:.1f}¢ (Pinnacle) · fee {cents(p['best_fee'])} · "
            f"edge {edge:g}¢ · {int(p['best_size']):,} contracts offered\n"
            f"Ticker {p['ticker']}"
            + ("".join(f"\n✅ Also value: {t}" for t in p.get("confirmed_by", [])))
            + early)
    return blocks
