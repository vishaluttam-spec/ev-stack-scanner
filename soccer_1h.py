#!/usr/bin/env python3
"""
Soccer first-half check for the EV Stack scanner.

Contract: Kalshi "1st Half Total — Over 0.5 goals" (a goal before halftime).

Each pass:
  1. Finds every open Kalshi soccer 1H Over 0.5 market kicking off soon.
  2. Matches it to the same game on The Odds API (by kickoff time + team names) and
     pulls Pinnacle's first-half total (totals_h1). Pinnacle's no-margin price is
     turned into a fair chance of a first-half goal with a Poisson goal model
     (handles 0.5 / 0.75 / 1.0 / 1.25 / 1.5 lines, incl. Asian quarter lines).
  3. Edge = fair - (Kalshi ask + Kalshi taker fee), for YES (a goal) and NO (0-0 at HT).
     Telegram alert: 🟢 ACTION 5c+, 🟡 ALERT 4c (same thresholds as the NFL add-on).
  4. Paper log (no money): every game is recorded with its last pre-kick price, the
     Pinnacle fair price, and later the result, in data/soccer_1h_log.json/.csv.
     This grades two rules side by side:
        "78% rule"   - buy YES whenever the Kalshi ask is 78c+
        "edge rule"  - buy only when Kalshi is 4c+ cheaper than Pinnacle (after fee)
  5. Weekly Telegram scorecard (Monday morning pass) for both rules.

Public data only: no Kalshi account, key or orders.
Odds API cost: 1 credit per matched game per Pinnacle pull (one region, one market).
Pinnacle is pulled at most once per game per REFETCH_MIN, only inside the alert window.
"""
from __future__ import annotations
import csv, json, math, os, re, sys, time, unicodedata
from datetime import datetime, timezone, timedelta

import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ODDS = "https://api.the-odds-api.com/v4"
DATA = "data"
LOG_JSON = f"{DATA}/soccer_1h_log.json"
LOG_CSV = f"{DATA}/soccer_1h_log.csv"
ALERTED = f"{DATA}/soccer_1h_alerted.json"
STATE = f"{DATA}/soccer_1h_state.json"

ACTION_C, ALERT_C = 5, 4
RULE_78 = 0.78
WINDOW_MIN = (15, 75)         # hourly run: each game is price-checked once, 15-75 min before kick
REFETCH_MIN = 50              # never re-pull Pinnacle for a game within 50 min
MIN_SIZE = 25                 # contracts on offer at the ask
REALERT_C = 2
MIN_CREDITS = 2500            # stop pulling Pinnacle if the Odds API balance gets low
SETTLE_AFTER_H = 3            # grade a game 3h after kickoff
STAKE = 10.0                  # flat $ per paper trade for the scorecard

ODDS_KEY = os.environ.get("ODDS_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ------------------------------------------------------------------ io
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)


def telegram(text):
    print("---- telegram ----\n" + text)
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text}, timeout=15)
    except Exception as e:
        print("telegram error:", e)


_last = [0.0]


def kget(path, **params):
    """Paced Kalshi GET with backoff on 429."""
    for attempt in range(7):
        wait = 0.35 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        r = requests.get(f"{KALSHI}{path}", params=params, timeout=20)
        if r.status_code == 429:
            time.sleep(min(2 ** attempt, 20))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Kalshi rate limit persisted on {path}")


_credits = [None]


def oget(path, **params):
    r = requests.get(f"{ODDS}{path}", params={"apiKey": ODDS_KEY, **params}, timeout=20)
    rem = r.headers.get("x-requests-remaining")
    if rem is not None:
        _credits[0] = int(float(rem))
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------ helpers
def fee(p):
    """Kalshi taker fee per contract, rounded up to the cent (same as kalshi.py)."""
    return math.ceil(round(7 * p * (1 - p), 6)) / 100


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def implied(dec):
    return 1.0 / dec


def pt(dt):
    """UTC datetime -> 'Wed 9:45 AM PT' (PDT/PST without tz libs)."""
    y = dt.year
    mar = datetime(y, 3, 8, 10, tzinfo=timezone.utc)
    mar += timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(y, 11, 1, 9, tzinfo=timezone.utc)
    nov += timedelta(days=(6 - nov.weekday()) % 7)
    off = -7 if mar <= dt < nov else -8
    return (dt + timedelta(hours=off)).strftime("%a %-I:%M %p") + " PT"


STOP = {"fc", "cf", "ac", "sc", "afc", "ssc", "as", "us", "cd", "ud", "sd", "rc", "rcd", "fk",
        "sk", "nk", "bk", "if", "ik", "club", "de", "the", "and", "cp", "sv", "vfb", "vfl",
        "tsg", "1", "04", "05", "calcio", "athletic", "sporting", "real",
        "atletico", "olympique", "kv", "krc", "rsc"}
ALIAS = {"bodoe": "bodo", "glimt": "glimt", "psv": "eindhoven", "inter": "internazionale",
         "man": "manchester", "utd": "united", "wolves": "wolverhampton", "spurs": "tottenham",
         "nottm": "nottingham", "qpr": "queens", "sheff": "sheffield", "weds": "wednesday",
         "leipzig": "leipzig", "rb": "", "gladbach": "monchengladbach", "psg": "paris"}


def tokens(name, keep_stop=False):
    s = name.lower()
    for a, b in (("ø", "o"), ("æ", "ae"), ("ß", "ss"), ("ł", "l"), ("đ", "d"), ("ı", "i"), ("&", " and ")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    out = set()
    for t in re.split(r"[^a-z0-9]+", s):
        t = ALIAS.get(t, t)
        if t and len(t) > 1 and (keep_stop or t not in STOP):
            out.add(t)
    return out


def _overlap(ta, tb):
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    if inter:
        return len(inter) / min(len(ta), len(tb))
    for x in ta:                 # prefix match, e.g. "bodo" vs "bodoe"
        for y in tb:
            if len(x) >= 4 and len(y) >= 4 and (x.startswith(y) or y.startswith(x)):
                return 0.6
    return 0.0


def team_score(a, b):
    s = _overlap(tokens(a), tokens(b))
    if s == 0:   # short Kalshi names such as "Atletico" or "Sporting CP"
        s = 0.5 * _overlap({t for t in tokens(a, True) if len(t) >= 4},
                           {t for t in tokens(b, True) if len(t) >= 4}) / max(1e-9, 1)
        s = min(s, 0.5)
    return s


def split_teams(text):
    m = re.split(r"\s+vs\.?\s+", text, maxsplit=1)
    return (m[0].strip(), m[1].strip()) if len(m) == 2 else (text, "")


# ------------------------------------------------------------------ Poisson model
def _pk(lam, k):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _component_ev(lam, line, dec_over):
    """EV per 1 unit staked on OVER `line` (x.0 or x.5) at decimal odds."""
    win = lose = 0.0
    for k in range(0, 15):
        p = _pk(lam, k)
        if k > line:
            win += p
        elif k < line:
            lose += p
    return win * (dec_over - 1) - lose


def over_ev(lam, line, dec_over):
    frac = round((line * 4) % 4)
    if frac in (1, 3):                        # quarter line: split stake
        return 0.5 * (_component_ev(lam, line - 0.25, dec_over) +
                      _component_ev(lam, line + 0.25, dec_over))
    return _component_ev(lam, line, dec_over)


def fair_goal_prob(line, over_dec, under_dec):
    """Pinnacle 1H total (line, decimal odds) -> fair P(at least one 1H goal)."""
    po = implied(over_dec) / (implied(over_dec) + implied(under_dec))
    fair_dec = 1 / po                          # margin-free decimal price on the over
    lo, hi = 0.02, 5.0
    for _ in range(80):                        # EV(over) rises with lambda
        mid = (lo + hi) / 2
        if over_ev(mid, line, fair_dec) > 0:
            hi = mid
        else:
            lo = mid
    lam = (lo + hi) / 2
    return 1 - math.exp(-lam), lam, po


# ------------------------------------------------------------------ Kalshi side
def kalshi_markets(now):
    """Open soccer 1H Over 0.5 markets kicking off within the next ~2 days."""
    series = [s["ticker"] for s in kget("/series", category="Sports").get("series", [])
              if "Soccer" in (s.get("tags") or []) and s["ticker"].endswith("1HTOTAL")]
    out = []
    for st in series:
        cursor = None
        for _ in range(5):
            p = {"series_ticker": st, "status": "open", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            d = kget("/markets", **p)
            for m in d.get("markets", []):
                if f(m.get("floor_strike")) != 0.5 or not m.get("occurrence_datetime"):
                    continue
                occ = datetime.fromisoformat(m["occurrence_datetime"].replace("Z", "+00:00"))
                kick = occ - timedelta(hours=4)    # Kalshi 1H markets: occurrence = kickoff + 4h
                if kick < now - timedelta(minutes=5) or kick > now + timedelta(days=2):
                    continue
                game = m.get("rules_primary", "").split("1st Half of the ")[-1].split(" professional")[0]
                game = game.split(" soccer game")[0].strip()
                out.append({"ticker": m["ticker"], "series": st, "league": st[2:-7],
                            "game": game, "kick": kick, "m": m})
            cursor = d.get("cursor")
            if not cursor:
                break
    return out


# ------------------------------------------------------------------ Odds API side
_events = {}


def odds_events(sport_keys):
    """Free endpoint: upcoming events per soccer sport key (cached per run)."""
    out = []
    for k in sport_keys:
        if k not in _events:
            try:
                _events[k] = oget(f"/sports/{k}/events")
            except requests.HTTPError as e:
                print("events", k, e.response.status_code)
                _events[k] = []
        for e in _events[k]:
            out.append((k, e))
    return out


def match_event(km, events):
    """Same kickoff (+-25 min) and both team names agree."""
    h, a = split_teams(km["game"])
    best, best_s = None, 0.0
    for key, e in events:
        ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        if abs((ct - km["kick"]).total_seconds()) > 25 * 60:
            continue
        p1 = (team_score(h, e["home_team"]), team_score(a, e["away_team"]))
        p2 = (team_score(h, e["away_team"]), team_score(a, e["home_team"]))
        s = max(sum(p) if min(p) > 0 else 0 for p in (p1, p2))
        if s > best_s:
            best, best_s = (key, e), s
    return best if best_s >= 1.0 else None


def pinnacle_1h(key, event_id):
    d = oget(f"/sports/{key}/events/{event_id}/odds", regions="eu", markets="totals_h1",
             bookmakers="pinnacle", oddsFormat="decimal")
    for bk in d.get("bookmakers", []):
        if bk["key"] != "pinnacle":
            continue
        for m in bk.get("markets", []):
            if m["key"] != "totals_h1":
                continue
            o = {x["name"]: x for x in m["outcomes"]}
            if "Over" in o and "Under" in o and o["Over"].get("point") is not None:
                return float(o["Over"]["point"]), float(o["Over"]["price"]), float(o["Under"]["price"])
    return None


# ------------------------------------------------------------------ grading / scorecard
def settle(log, now):
    n = 0
    for t, r in log.items():
        if r.get("result") or not r.get("kick"):
            continue
        kick = datetime.fromisoformat(r["kick"])
        if now - kick < timedelta(hours=SETTLE_AFTER_H):
            continue
        try:
            m = kget(f"/markets/{t}").get("market", {})
        except Exception as e:
            print("settle", t, e)
            continue
        if m.get("result") in ("yes", "no"):
            r["result"] = m["result"]
            n += 1
    return n


def pnl(price, won, side_yes=True):
    """$ P&L of a flat STAKE buying at `price` (+ fee)."""
    cost = price + fee(price)
    contracts = STAKE / cost
    return contracts * (1 - cost) if won else -STAKE


def scorecard(log, since=None):
    rows = [r for r in log.values() if r.get("result") and r.get("ask")]
    if since:
        rows = [r for r in rows if r["kick"] >= since.isoformat()]
    out = []
    # 78% rule
    r78 = [r for r in rows if r["ask"] >= RULE_78]
    if r78:
        w = sum(r["result"] == "yes" for r in r78)
        avg = sum(r["ask"] for r in r78) / len(r78)
        be = sum(r["ask"] + fee(r["ask"]) for r in r78) / len(r78)
        p = sum(pnl(r["ask"], r["result"] == "yes") for r in r78)
        out.append(f"78% rule: {w}-{len(r78) - w} ({100 * w / len(r78):.0f}% hit, avg price "
                   f"{avg * 100:.0f}¢, needed {be * 100:.0f}%) → ${p:+.2f} on ${STAKE:.0f} each")
    else:
        out.append("78% rule: no graded games yet")
    # edge rule (either side)
    re_ = [r for r in rows if (r.get("edge_c") or -99) >= ALERT_C]
    if re_:
        w = 0
        p = 0.0
        for r in re_:
            yes = r["edge_side"] == "YES"
            won = (r["result"] == "yes") == yes
            w += won
            p += pnl(r["edge_ask"], won)
        out.append(f"Pinnacle edge rule (4¢+): {w}-{len(re_) - w} → ${p:+.2f} on ${STAKE:.0f} each")
    else:
        out.append("Pinnacle edge rule (4¢+): no graded signals yet")
    # calibration: Kalshi price vs outcome
    if rows:
        tot = sum(r["result"] == "yes" for r in rows)
        avg = sum(r["ask"] for r in rows) / len(rows)
        out.append(f"All games logged: {len(rows)}, goal before HT {100 * tot / len(rows):.0f}% "
                   f"vs avg Kalshi ask {avg * 100:.0f}¢")
        pf = [r for r in rows if r.get("fair_yes") is not None]
        if pf:
            g = sum(r["result"] == "yes" for r in pf)
            out.append(f"Pinnacle model check: {len(pf)} games, said {100 * sum(r['fair_yes'] for r in pf) / len(pf):.0f}%, "
                       f"actual {100 * g / len(pf):.0f}%")
    return out


def write_csv(log):
    cols = ["kick", "league", "game", "ask", "bid", "size", "pin_line", "pin_over", "pin_under",
            "fair_yes", "edge_side", "edge_ask", "edge_c", "result", "ticker"]
    with open(LOG_CSV, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for t, r in sorted(log.items(), key=lambda kv: kv[1].get("kick", "")):
            w.writerow([r.get(c, "") if c != "ticker" else t for c in cols])


# ------------------------------------------------------------------ main
def run(now=None, probe=False):
    now = now or datetime.now(timezone.utc)
    os.makedirs(DATA, exist_ok=True)
    log = load_json(LOG_JSON, {})
    alerted = load_json(ALERTED, {})
    state = load_json(STATE, {})

    markets = kalshi_markets(now)
    print(f"{len(markets)} open Kalshi 1H Over 0.5 markets in the next 2 days")

    # soccer sport keys on the Odds API (free call)
    sport_keys = []
    if ODDS_KEY:
        try:
            sport_keys = [s["key"] for s in oget("/sports") if s["key"].startswith("soccer_")]
        except Exception as e:
            print("sports list error:", e)
    print(f"{len(sport_keys)} soccer competitions on the Odds API")

    alerts, matched, pulled, unmatched = [], 0, 0, []
    lo, hi = (5, 36 * 60) if probe else WINDOW_MIN      # probe: test coverage on later games too
    window = {km["ticker"] for km in markets
              if lo <= (km["kick"] - now).total_seconds() / 60 <= hi}
    max_pulls = 4 if probe else 999
    events = odds_events(sport_keys) if (window and sport_keys) else []

    for km in markets:
        m = km["m"]
        mins = (km["kick"] - now).total_seconds() / 60
        ask, bid = f(m.get("yes_ask_dollars")), f(m.get("yes_bid_dollars"))
        if ask is None or ask <= 0 or ask >= 1:
            continue
        rec = log.setdefault(km["ticker"], {})
        rec.update(league=km["league"], game=km["game"], kick=km["kick"].isoformat())
        if mins >= 0:                       # keep the last pre-kick price
            rec.update(ask=ask, bid=bid, size=f(m.get("yes_ask_size_fp")) or 0.0,
                       priced_at=now.isoformat(timespec="seconds"), mins_to_kick=round(mins))
        if km["ticker"] not in window:
            continue

        hit = match_event(km, events) if events else None
        if not hit:
            unmatched.append(km["game"])
            continue
        matched += 1
        key, ev = hit
        rec["odds_event"] = f"{key}/{ev['id']}"
        last = state.get(km["ticker"])
        stale = (not last or now - datetime.fromisoformat(last) > timedelta(minutes=REFETCH_MIN))
        if stale and pulled < max_pulls and (_credits[0] is None or _credits[0] >= MIN_CREDITS):
            try:
                pin = pinnacle_1h(key, ev["id"])
                pulled += 1
                state[km["ticker"]] = now.isoformat(timespec="seconds")
            except requests.HTTPError as e:
                print("pinnacle", km["game"], e.response.status_code)
                pin = None
            print("Pinnacle 1H", km["game"], "->", pin)
            if pin:
                line, o, u = pin
                fy, lam, po = fair_goal_prob(line, o, u)
                rec.update(pin_line=line, pin_over=o, pin_under=u, fair_yes=round(fy, 4),
                           lam=round(lam, 3))
        fy = rec.get("fair_yes")
        if fy is None:
            continue
        no_ask = 1 - bid if bid else None
        best = None
        for side, a, fair, size in (("YES", ask, fy, f(m.get("yes_ask_size_fp")) or 0),
                                    ("NO", no_ask, 1 - fy, f(m.get("yes_bid_size_fp")) or 0)):
            if a is None or a <= 0 or a >= 1:
                continue
            edge = round((fair - a - fee(a)) * 100, 1)
            if best is None or edge > best[1]:
                best = (side, edge, a, size, fair)
        side, edge, a, size, fair = best
        rec.update(edge_side=side, edge_c=edge, edge_ask=a)
        in_alert_window = WINDOW_MIN[0] <= mins <= WINDOW_MIN[1]
        if edge >= ALERT_C and size >= MIN_SIZE and in_alert_window:
            sid = f"{km['ticker']}|{side}"
            if alerted.get(sid) is None or edge >= alerted[sid] + REALERT_C:
                alerted[sid] = edge
                tag = "🟢 ACTION" if edge >= ACTION_C else "🟡 ALERT"
                what = ("YES — a goal before halftime" if side == "YES"
                        else "NO — 0-0 at halftime")
                alerts.append((edge,
                    f"{tag} {km['league']} {km['game']}\n"
                    f"Kick: {pt(km['kick'])} (in {round(mins)} min)\n"
                    f"➡️ Buy {what} at {a * 100:.0f}¢ (fee {fee(a) * 100:.0f}¢)\n"
                    f"Fair {fair * 100:.1f}¢ from Pinnacle 1H {rec['pin_line']:g} "
                    f"(over {rec['pin_over']:.2f} / under {rec['pin_under']:.2f}) · edge {edge:g}¢"))

    settled = settle(log, now)
    save_json(LOG_JSON, log)
    save_json(ALERTED, alerted)
    # drop refetch stamps for games long gone
    state = {t: s for t, s in state.items()
             if t.startswith("_") or now - datetime.fromisoformat(s) < timedelta(days=2)}
    save_json(STATE, state)
    write_csv(log)

    if probe:
        telegram(f"🔧 Soccer 1st-half check installed. Test run: {len(markets)} Kalshi games found, "
                 f"{matched} matched to the Odds API, {pulled} Pinnacle pulls, "
                 f"{sum(1 for t in window if log.get(t, {}).get('fair_yes') is not None)} with a Pinnacle "
                 f"1st-half price. Credits left: {_credits[0]}.")
    print(f"window {len(window)}, matched {matched}, Pinnacle pulls {pulled}, "
          f"settled {settled}, credits {_credits[0]}")
    if unmatched:
        print("unmatched:", "; ".join(unmatched[:20]))

    if alerts:
        alerts.sort(key=lambda x: -x[0])
        telegram("⚽ EV Stack SOCCER 1st-half edge\n(Kalshi vs Pinnacle, after fee — "
                 "signal only, confirm price before trading)\n\n" + "\n\n".join(t for _, t in alerts[:8]))

    # weekly scorecard: Monday pass between 14:00 and 16:59 UTC, once per week
    wk = now.strftime("%G-W%V")
    if now.weekday() == 0 and 14 <= now.hour < 17 and state.get("_scorecard") != wk:
        lines = scorecard(log)
        week = scorecard(log, since=now - timedelta(days=7))
        telegram("📊 Soccer 1st-half paper test (no money)\n\nLast 7 days:\n" + "\n".join(week)
                 + "\n\nSince start:\n" + "\n".join(lines))
        state["_scorecard"] = wk
        save_json(STATE, state)
    return alerts


if __name__ == "__main__":
    run(probe="--probe" in sys.argv)
