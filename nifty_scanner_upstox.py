"""
Nifty 50 multi-signal scanner v8.9 (Upstox) -> Telegram alerts. Places NO orders.
Educational tool only. Not financial advice.

v8.9 change: every option reading is saved to option_YYYY-MM-DD.csv (OPTION_CSV, every 5 minutes, one file a day) with
time, expiry, spot, ATM strike, near-the-money call/put OI, PCR (near and whole chain), biggest call/put OI strikes,
ATM IV and the call/put OI change since the first reading of the day. Send these files with the nifty_ CSVs to backtest.

v8.8 change: OPTION CHAIN context on CALL/PUT alerts (information only): near-the-money PCR, biggest call/put OI
strikes (resistance/support), OI change since the first reading of the day, ATM IV. Flags "against this signal" when a
CALL sits just under big call OI, a PUT just over big put OI, or OI is building against the signal. Fetched from the
Upstox option chain (v2/option/chain) every 5 minutes. NOT backtested (your CSV files have no option data) and the
live API call was not tested here, so it is context only. If the fetch fails the line is just left out.

v8.7 change: VOLUME + PRICE combination (6 cases: volume up/down x price up/flat/down), information only.
It adds one line to CALL/PUT, TREND and VOLUME SPIKE messages and sends its own message when the combination
changes and holds 3 candles (about 6 a day). A line says "against this signal" when the combination points the
other way. Replay of 27 days: on their own the combinations did NOT predict the next 15 minutes (about 50% up
in every case), so they are context, never a signal. Settings: COMBO_* below.

v8.6 change: VOLUME SPIKE alert to Telegram (information only). Sent when a candle's futures volume is at least
VOL_ALERT_X (4.0) times the 20-candle average, at most one per 15 minutes, not in the first 15 minutes. It says
whether the candle was buying (up) or selling (down), the volume, net volume and RSI. It never changes a CALL/PUT signal.
Edit the VOLUME SPIKE settings below (VOLUME_ALERT = False turns it off).
v8.5 change: no time blocks, alerts from 09:15 to 15:30.

v8.4 change: accuracy tuning from a replay of 27 trading days (1 Oct - 13 Nov 2026). Win = +30 pts before -30 pts
within 60 minutes (a symmetric test, so ~50% is chance). Same-code replay, raw signals before any filter:
  BREAKOUT 63% (39) | PULLBACK 62% (86) | OPENING BREAK 60% (10) | SWING 54% (246) | FAKE BREAKOUT 42% | TREND REVERSE 51% | INDICATION 50%
  - PULLBACK is now sent live (best signal in the replay), only 10:00-14:30 (before 10:00 it won 45%, after 14:00 40%)
  - the chop filter (3+ VWAP or EMA9/21 crosses in 30 candles) now also blocks BREAKOUT and PULLBACK:
    BREAKOUT 63% -> 72%, PULLBACK 62% -> 69%
  - the opposite-direction cooldown (30 min) now also applies to BREAKOUT
  - live set after filters: about 3 alerts a day, about 68% won, Nov 2-13 (7 days) 91% on 11 alerts
  SWING, FAKE BREAKOUT, TREND REVERSE and INDICATION stay summary-only. Small sample, no costs or slippage, index points.

v8.3 change: ACCURACY MODE (ACCURACY_MODE = True). Fewer, stricter alerts, built for a range day like 9 Nov 2026 where
the older rules flipped CALL/PUT about 20 times. Not back-tested yet, so check it on your CSV days before trusting it.
  - only OPENING BREAK and BREAKOUT are sent live (the two with the best past numbers); SWING, PULLBACK and FAKE
    BREAKOUT are counted in the end-of-day summary only
  - OPENING BREAK is skipped when its stop (range middle) is more than OB_MAX_RISK_PTS from the entry (a spiky
    first candle makes the range far too wide)
  - no alert in the opposite direction within FLIP_COOLDOWN_MIN minutes of the last live alert (BREAKOUT is exempt and always sends)
  - SWING / PULLBACK / FAKE BREAKOUT are blocked when the last CHOP_WINDOW candles crossed VWAP or EMA9/EMA21
    CHOP_MAX_CROSSES times or more (only matters if you add them back to LIVE_KINDS)
  - SWING / PULLBACK / FAKE BREAKOUT against price vs the day's open are blocked instead of tagged (BREAKOUT is exempt)
  Set ACCURACY_MODE = False to get the v8.2 behaviour back.

v8.2 change: NET BUYING / SELLING VOLUME (context only, no signal rule changed). Futures volume of each 1-minute
candle is counted as buying if the futures candle closed above the previous close and as selling if below.
  - alerts and the TREND message carry "Net volume last 15m" and say when the flow is against the signal
  - the per-candle console line shows the same figure
  - the end-of-day summary lists net volume hour by hour next to the price move, and the high-volume candles
    (2x+) that came at an RSI extreme (possible exhaustion)
  Why: on 9 Nov 2026 the volume spikes alone did not give direction (correlation with the move's direction about
  0.06), the sustained net flow did. One day only, a rough proxy (a whole candle is given to one side), so it is
  shown, not used as a filter. Tune the FLOW_* settings.

v8.1 changes (checked on 23 trading days, 1 Oct - 9 Nov 2026):
  - TREND REVERSE is no longer sent live: 33 alerts, 36% won, -286 pts (the last 5 on 9 Nov all lost). Still counted in the summary.
  - SWING alerts that go against price vs the day's open are tagged "lower odds" (51% won with the open, 38% against).

v8 change: new SWING alert (top/bottom reversal). Replay of the 12 days (8-26 Oct 2026), held until the next swing
signal or 15:00, stop 5 pts beyond the swing extreme, no new swing alert after 13:30:
  - a 40-pt reversal off a swing bottom = BUY CALL, off a swing top = BUY PUT (the two sides must alternate)
  - 89 signals, about 44% won, +1,121 pts in total; positive on 10 of 12 days. Small sample, chosen on the same
    12 days, no costs or slippage, index points (not option premium). The profit comes from a few big runs.
  - it is late by design: a top or bottom is only confirmed after SWING_PTS of reversal. Tune the SWING_* settings.
  - a swing alert also tells you when it flips an open swing idea, a "SWING STOP HIT" alert follows if the stop
    is touched, and an exit idea is sent at 15:00.
  Everything else is unchanged from v7.

v7 changes, all based on a replay of 12 trading days (8-26 Oct 2026). Small sample, so re-test on new days:
  1. TIME RULES   no new alert after 13:30 (25 signals after 13:30 lost 317 pts net at 60 min),
                  and no BREAKOUT after 12:00 (before 12:00: 10 of 13 won; after 12:00 it was flat or negative).
  2. INDICATION   no longer sent live (10 signals, 3 won, -637 pts at 60 min). Still counted in the end-of-day summary.
  3. OPENING BREAK now sent live, with a stop at the middle of the first-15-minute range and an exit idea at 15:15
                  (10 trades, 7 won, +1,179 pts held to 15:15). A "stop hit" alert follows if price touches the stop.
  4. The day's high or low was set by 09:48 on all 12 days, so the opening-range extremes are the levels that matter.

Alert types (each labelled with a confidence level):
  OPENING BREAK  MEDIUM   first close beyond the 09:15-09:29 range before 10:00 (earliest entry, has a stop idea)
  BREAKOUT       HIGH     strong, high-volume break of the day's range that holds 2 candles (before 12:00 only)
  FAKE BREAKOUT  LOWER    high-volume break that fails and closes back inside the range
  PULLBACK       LOWER    trend continuation: bounce against the trend fails
  TREND REVERSE  summary only (not sent live since v8.1)  fresh EMA cross + VWAP flip + break of recent structure
  SWING          MEDIUM   40-pt reversal off a day top (PUT) or bottom (CALL), held to the next swing signal (v8)
  INDICATION     summary  early read on 8-24 candles, counted at end of day only
  TREND          info     UP / DOWN status when the weak-trend score holds 10 min (context, not an entry)

Env vars: UPSTOX_ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import gzip
import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

INDEX_KEY = "NSE_INDEX|Nifty 50"
INSTR_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
REFRESH_SECONDS = 60

# ---- v7 time rules (edit to tune) ----
NO_NEW_AFTER = (15, 30)      # alerts allowed through the whole session (09:15 to 15:30)
BREAKOUT_LAST = (15, 30)      # no BREAKOUT time limit

# ---- v8 SWING top/bottom settings (edit to tune) ----
SWING_PTS = 40               # reversal, in points, off the extreme that confirms a top or bottom
SWING_MIN_BARS = 5           # the extreme must be at least this many candles old
SWING_COOLDOWN_MIN = 10      # minutes between swing signals
SWING_STOP_BUFFER = 5        # stop idea = swing extreme +/- this many points
SWING_EMA_CONFIRM = False    # True: CALL needs close above EMA9, PUT needs close below (no consistent gain in the replay)
SWING_START = (9, 20)        # swing tracking starts here (the first 5 candles are ignored)
SWING_EXIT = (15, 0)         # exit idea for an open swing idea; also the last candle used in the end-of-day result

# ---- v8.2 net buying/selling volume (context only, edit to tune) ----
FLOW_WINDOW = 15             # candles used for the rolling "net volume" figure in alerts
FLOW_DOM_PCT = 25            # net volume of this % of total or more = buyers (or sellers) dominating
FLOW_HV_X = 2.0              # a "high-volume" candle is at least this many times the 20-candle average
FLOW_RSI_LOW, FLOW_RSI_HIGH = 25, 80   # high volume at these RSI extremes is listed as possible exhaustion

# ---- v8.6 VOLUME SPIKE alert to Telegram (information only, not a CALL/PUT signal) ----
VOLUME_ALERT = True          # send a Telegram message when futures volume spikes
VOL_ALERT_X = 4.0            # candle volume >= this many times the 20-candle average (3.0 = about 9 a day, 4.0 = about 6)
VOL_ALERT_COOLDOWN_MIN = 15  # minutes between volume alerts
VOL_ALERT_MIN_CANDLES = 15   # not in the first 15 minutes (the open is always heavy)

# ---- v8.7 VOLUME + PRICE combination (6 cases, information only) ----
COMBO_WINDOW = 3             # candles used: average volume ratio and price change over the last 3 candles
COMBO_VOL_UP_X = 1.5         # volume UP = average volume ratio at or above this
COMBO_VOL_DOWN_X = 0.7       # volume DOWN = at or below this (in between = normal volume, no combination)
COMBO_FLAT_PCT = 0.05        # price FLAT = move within +/- this % (about 12 pts)
COMBO_IN_ALERTS = True       # add the combination line to CALL/PUT, TREND and VOLUME SPIKE messages
COMBO_ALERT = True           # also send a Telegram message when the combination changes
COMBO_HOLD = 3               # the new combination must last this many candles before it is sent
COMBO_GAP_MIN = 15           # minutes between combination messages
COMBO_SEND = ("UU", "UF", "UD", "DU", "DD")   # U/D = volume up/down, then u/f/d = price up/flat/down; "Df" (quiet) is not sent
COMBOS = {
    "Uu": ("Volume UP + price UP", "strong buying, the rise has support", 1),
    "Uf": ("Volume UP + price FLAT", "heavy trading but price is stuck: big players absorbing at this level, a breakout either way may follow", 0),
    "Ud": ("Volume UP + price DOWN", "strong selling, the fall has support", -1),
    "Du": ("Volume DOWN + price UP", "weak rise, few buyers behind it, it can fade", 0),
    "Df": ("Volume DOWN + price FLAT", "quiet, no interest, wait", 0),
    "Dd": ("Volume DOWN + price DOWN", "weak fall, few sellers behind it, may pause or bounce", 0),
}

# ---- v8.8 OPTION CHAIN context: OI, PCR, IV (information only, never blocks or creates a signal) ----
OPTION_DATA = True           # add an options line to CALL/PUT alerts (silently skipped if the option data cannot be fetched)
OPTION_CSV = True            # v8.9: save every option reading to option_YYYY-MM-DD.csv (for backtesting OI / PCR later)
OPTION_CSV_DIR = "."         # folder for the option CSV
OPT_REFRESH_SEC = 300        # fetch the option chain at most every 5 minutes
OPT_NEAR_STRIKES = 6         # strikes on each side of the money used for near-the-money OI and PCR
OPT_WALL_NEAR_PTS = 40       # a CALL this close under the biggest call OI strike (or a PUT this close over the biggest put OI strike) is flagged
OPT_PCR_BULL, OPT_PCR_BEAR = 1.2, 0.8   # near-the-money PCR at or above / below these = put / call writers dominating
OPT_OI_SHIFT_PCT = 5         # call or put OI change since the first reading of the day must differ by this many % points to be called out

# ---- Strong-breakout settings ----
OPENING_RANGE_MIN = 15       # first 15 minutes = opening range
LEVEL_WINDOW = 30            # also must break the last 30 candles' high/low
BREAKOUT_BUFFER_PCT = 0.05   # close must be this % beyond the level (~11 pts)
BODY_FRAC = 0.60             # breakout candle body >= 60% of its range
RANGE_MULT = 1.2             # breakout candle range >= 1.2x average
VOL_BREAKOUT_RATIO = 1.5     # futures volume >= 1.5x average on breakout candle
HOLD_MIN = 2                 # candles that must hold beyond the level after breakout
MAX_EXTENSION_PCT = 0.25     # skip if price already ran this % past the level
RANGE_WINDOW = 30            # candles used for the 'is it moving' check
MIN_RANGE_PCT = 0.15         # skip if last 30-min range is below this %
COOLDOWN_MIN = 15            # minutes between alerts
LATE_GRACE = 2               # still accept a breakout candle up to 2 candles older (covers skipped minutes)
OBSERVE_TELEGRAM_EVERY = 0   # silent price+volume update to Telegram every N minutes (0 = off)

# ---- Early INDICATION (summary only in v7) ----
IND_MIN_CANDLES = 8
FULL_CANDLES = 25
IND_EXT_WINDOW = 8
IND_FLIP_WINDOW = 6
IND_INVALID_WINDOW = 6
IND_COOLDOWN_MIN = 3

# ---- Extra signal types ----
FAKE_WINDOW = 8
FAKE_VOL_RATIO = 1.5
FAKE_MARGIN_PCT = 0.05
PULL_BOUNCE_WINDOW = 6
PULL_NO_ENTRY_AFTER = (15, 30)
PULL_NO_ENTRY_BEFORE = (9, 15)   # no pullback time limit
PULL_MIN_RANGE_PCT = 0.20
REV_CROSS_WINDOW = 10
REV_VWAP_WINDOW = 15
REV_STRUCT_WINDOW = 15
REV_VOL_RATIO = 1.2
WATCH_PCT = 0.08
WATCH_VOL_RATIO = 3.0
WATCH_TIMEOUT_MIN = 10
WATCH_COOLDOWN_MIN = 15

# ---- TREND status message (context only, not an entry signal) ----
SEND_TREND = True
TREND_HOLD_MIN = 10

# ---- GitHub relay: a GitHub job is stopped after 6 hours, so the scanner hands over to a fresh run ----
MAX_RUN_MIN = float(os.environ.get("SCAN_MAX_MINUTES", "0") or 0)   # 0 = never hand over (server use)

# ---- What is sent live vs only counted in the end-of-day summary ----
# ---- v8.3 ACCURACY MODE: fewer, stricter alerts (False = v8.2 behaviour) ----
ACCURACY_MODE = True
LIVE_KINDS = (("OPENING BREAK", "BREAKOUT", "PULLBACK") if ACCURACY_MODE
              else ("OPENING BREAK", "BREAKOUT", "FAKE BREAKOUT", "PULLBACK", "SWING"))
OB_MAX_RISK_PTS = 60         # skip OPENING BREAK if the stop (range middle) is further than this from the entry
FLIP_COOLDOWN_MIN = 30       # no live alert in the opposite direction within this many minutes of the last live alert
CHOP_WINDOW = 30             # candles checked for chop
CHOP_MAX_CROSSES = 3         # this many VWAP or EMA9/21 crosses in the window = choppy, block
CHOP_KINDS = ("BREAKOUT", "PULLBACK", "SWING", "FAKE BREAKOUT")   # v8.4: BREAKOUT and PULLBACK are chop-filtered too
SEND_WAIT_NOTES = False

# ---- Alignment filter: signals that go against price vs the day's open ----
ALIGN_MODE = "block" if ACCURACY_MODE else "tag"   # "tag" = send but mark "against the open"; "block" = skip them; "off" = ignore
ALIGN_KINDS = tuple(k for k in ("INDICATION", "BREAKOUT", "FAKE BREAKOUT", "PULLBACK", "SWING")
                    if not (ACCURACY_MODE and k == "BREAKOUT"))   # a BREAKOUT already needs price on the right side of VWAP and EMAs

# ---- Opening read (09:30) and OPENING BREAK ----
SEND_OPENING_READ = True
OPENING_BREAK_UNTIL = (10, 0)   # first close beyond the first-15-minute range, before this time
OB_EXIT = (15, 15)              # exit idea for an opening break

CALL, PUT, WAIT = "BUY CALL (CE)", "BUY PUT (PE)", "WAIT / NO TRADE"
IST = timezone(timedelta(hours=5, minutes=30))
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(text: str, silent: bool = False) -> None:
    if not (TG_TOKEN and TG_CHAT_ID):
        print("  (Telegram not configured, skipping alert)")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "disable_notification": silent},
            timeout=10,
        )
        if not r.ok:
            print(f"  Telegram error {r.status_code}: {r.text}")
    except requests.RequestException as e:
        print(f"  Telegram send failed: {e}")


def fetch_candles(key: str) -> pd.DataFrame:
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        print("Set the UPSTOX_ACCESS_TOKEN environment variable first.")
        raise SystemExit(3)
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{quote(key, safe='')}/minutes/1"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code == 401:
        print("Token expired or invalid. Generate a new Upstox access token.")
        send_telegram("Upstox token expired or invalid, so the scanner stopped. "
                      "Update the token and it will restart by itself.")
        raise SystemExit(3)
    r.raise_for_status()
    candles = r.json()["data"]["candles"]  # [ts, o, h, l, c, volume, oi], newest first
    if not candles:  # no candles yet (before the first one, or no trading today)
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"],
                            index=pd.DatetimeIndex([], tz=IST))
    df = pd.DataFrame(candles).iloc[:, :6]
    df.columns = ["Time", "Open", "High", "Low", "Close", "Volume"]
    df["Time"] = pd.to_datetime(df["Time"])
    df = df.sort_values("Time").set_index("Time")
    now_min = datetime.now(IST).replace(second=0, microsecond=0)
    if len(df) and df.index[-1].to_pydatetime() >= now_min:
        df = df.iloc[:-1]  # drop the still-forming candle
    return df


def get_future_key():
    """Nifty index futures contract: nearest expiry, skipping the last 2 days."""
    r = requests.get(INSTR_URL, timeout=60)
    r.raise_for_status()
    data = json.loads(gzip.decompress(r.content))
    cutoff = (datetime.now(IST) + timedelta(days=2)).timestamp() * 1000
    futs = [i for i in data
            if i.get("segment") == "NSE_FO" and i.get("instrument_type") == "FUT"
            and i.get("underlying_symbol") == "NIFTY" and i.get("expiry", 0) > cutoff]
    if not futs:
        return None, None
    f = min(futs, key=lambda i: i["expiry"])
    return f["instrument_key"], f.get("trading_symbol", "NIFTY FUT")


_OPT = {"expiry": None, "day": None, "t": None, "snap": None, "first": None}


def option_expiry():
    """Nearest NIFTY option expiry that is today or later (from the same instruments file as the futures)."""
    today = datetime.now(IST).date()
    if _OPT["day"] == today and _OPT["expiry"]:
        return _OPT["expiry"]
    r = requests.get(INSTR_URL, timeout=60)
    r.raise_for_status()
    data = json.loads(gzip.decompress(r.content))
    start = datetime.combine(today, datetime.min.time(), tzinfo=IST).timestamp() * 1000
    exps = sorted({i["expiry"] for i in data
                   if i.get("segment") == "NSE_FO" and i.get("instrument_type") in ("CE", "PE")
                   and i.get("underlying_symbol") == "NIFTY" and i.get("expiry", 0) >= start})
    if not exps:
        return None
    _OPT["expiry"] = datetime.fromtimestamp(exps[0] / 1000, IST).strftime("%Y-%m-%d")
    _OPT["day"] = today
    return _OPT["expiry"]


def parse_option_chain(rows, spot=None):
    """rows: Upstox option-chain data list. Returns a dict of near-the-money OI, PCR, walls and ATM IV, or None."""
    recs = []
    for x in rows or []:
        k = x.get("strike_price")
        c, p = x.get("call_options") or {}, x.get("put_options") or {}
        cm, pm = c.get("market_data") or {}, p.get("market_data") or {}
        if k is None:
            continue
        recs.append(dict(k=float(k), c_oi=float(cm.get("oi") or 0), p_oi=float(pm.get("oi") or 0),
                         c_iv=(c.get("option_greeks") or {}).get("iv"), p_iv=(p.get("option_greeks") or {}).get("iv"),
                         spot=x.get("underlying_spot_price")))
    if not recs:
        return None
    t = pd.DataFrame(recs).sort_values("k").reset_index(drop=True)
    sp = spot or next((v for v in t["spot"] if v), None)
    if not sp:
        return None
    atm = int((t["k"] - sp).abs().idxmin())
    near = t.iloc[max(0, atm - OPT_NEAR_STRIKES): atm + OPT_NEAR_STRIKES + 1]
    c_near, p_near = near["c_oi"].sum(), near["p_oi"].sum()
    above, below = t[t["k"] >= sp], t[t["k"] <= sp]
    ivs = [v for v in (t.loc[atm, "c_iv"], t.loc[atm, "p_iv"]) if v]
    return dict(spot=sp, atm=float(t.loc[atm, "k"]), c_near=c_near, p_near=p_near,
                pcr_near=p_near / c_near if c_near else None,
                pcr_all=t["p_oi"].sum() / t["c_oi"].sum() if t["c_oi"].sum() else None,
                call_wall=float(above.loc[above["c_oi"].idxmax(), "k"]) if len(above) and above["c_oi"].max() > 0 else None,
                put_wall=float(below.loc[below["p_oi"].idxmax(), "k"]) if len(below) and below["p_oi"].max() > 0 else None,
                iv=sum(ivs) / len(ivs) if ivs else None)


def log_option_csv(snap, now):
    """Append one row per reading to option_YYYY-MM-DD.csv (header written once). Never raises."""
    if not OPTION_CSV:
        return
    try:
        first = _OPT["first"] or {}
        def chg(a, b):
            return round((a / b - 1) * 100, 2) if b else ""
        row = {"Time": now.strftime("%Y-%m-%d %H:%M:%S"), "expiry": snap.get("expiry"), "spot": snap["spot"],
               "atm": snap["atm"], "call_oi_near": snap["c_near"], "put_oi_near": snap["p_near"],
               "pcr_near": round(snap["pcr_near"], 3) if snap["pcr_near"] is not None else "",
               "pcr_all": round(snap["pcr_all"], 3) if snap["pcr_all"] is not None else "",
               "call_wall": snap["call_wall"] or "", "put_wall": snap["put_wall"] or "",
               "atm_iv": round(snap["iv"], 2) if snap["iv"] else "",
               "call_oi_chg_pct": chg(snap["c_near"], first.get("c_near")),
               "put_oi_chg_pct": chg(snap["p_near"], first.get("p_near"))}
        path = os.path.join(OPTION_CSV_DIR, f"option_{now:%Y-%m-%d}.csv")
        new = not os.path.exists(path)
        pd.DataFrame([row]).to_csv(path, mode="a", header=new, index=False)
    except Exception as e:
        print(f"  Option CSV not saved: {e}")


def option_snapshot(spot):
    """Cached option-chain summary, refreshed every OPT_REFRESH_SEC. None if anything fails (never raises)."""
    try:
        now = datetime.now(IST)
        if _OPT["snap"] and _OPT["t"] and (now - _OPT["t"]).total_seconds() < OPT_REFRESH_SEC:
            return _OPT["snap"]
        if _OPT.get("fail_t") and (now - _OPT["fail_t"]).total_seconds() < OPT_REFRESH_SEC:
            return _OPT["snap"]      # a recent fetch failed: do not retry on every candle
        _OPT["fail_t"] = now
        exp = option_expiry()
        token = os.environ.get("UPSTOX_ACCESS_TOKEN")
        if not exp or not token:
            return None
        r = requests.get("https://api.upstox.com/v2/option/chain",
                         params={"instrument_key": INDEX_KEY, "expiry_date": exp},
                         headers={"Accept": "application/json", "Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        snap = parse_option_chain(r.json().get("data"), spot)
        if snap:
            snap["expiry"] = exp
            if _OPT["first"] is None or _OPT["first"].get("day") != now.date():
                _OPT["first"] = dict(day=now.date(), c_near=snap["c_near"], p_near=snap["p_near"])
            _OPT["snap"], _OPT["t"] = snap, now
            _OPT["fail_t"] = None
            log_option_csv(snap, now)
        return snap
    except Exception as e:     # option data is optional context: never break the scanner
        print(f"  Option chain unavailable: {e}")
        return None


def option_note(price, signal=None) -> str:
    """Options line(s) for a CALL/PUT alert, ending in a newline; empty if the option data is unavailable."""
    if not OPTION_DATA:
        return ""
    o = option_snapshot(price)
    if not o:
        return ""
    parts = []
    if o["pcr_near"] is not None:
        who = ("put writers dominating (support)" if o["pcr_near"] >= OPT_PCR_BULL
               else "call writers dominating (resistance)" if o["pcr_near"] <= OPT_PCR_BEAR else "balanced")
        parts.append(f"PCR near the money {o['pcr_near']:.2f}, {who}")
    if o["call_wall"] or o["put_wall"]:
        parts.append(f"Biggest OI: call {o['call_wall']:,.0f} (resistance), put {o['put_wall']:,.0f} (support)"
                     if o["call_wall"] and o["put_wall"] else
                     f"Biggest OI: {'call ' + format(o['call_wall'], ',.0f') if o['call_wall'] else 'put ' + format(o['put_wall'], ',.0f')}")
    first = _OPT["first"]
    flags = []
    if first and first["c_near"] and first["p_near"]:
        dc = (o["c_near"] / first["c_near"] - 1) * 100
        dp = (o["p_near"] / first["p_near"] - 1) * 100
        parts.append(f"Near-the-money OI since first reading today: calls {dc:+.0f}%, puts {dp:+.0f}%")
        if dp - dc >= OPT_OI_SHIFT_PCT and signal == PUT:
            flags.append("put OI is building faster than call OI (support) <- against a PUT")
        if dc - dp >= OPT_OI_SHIFT_PCT and signal == CALL:
            flags.append("call OI is building faster than put OI (resistance) <- against a CALL")
    if signal == CALL and o["call_wall"] and 0 <= o["call_wall"] - price <= OPT_WALL_NEAR_PTS:
        flags.append(f"price is only {o['call_wall'] - price:.0f} pts under the big call OI at {o['call_wall']:,.0f} <- against a CALL")
    if signal == PUT and o["put_wall"] and 0 <= price - o["put_wall"] <= OPT_WALL_NEAR_PTS:
        flags.append(f"price is only {price - o['put_wall']:.0f} pts over the big put OI at {o['put_wall']:,.0f} <- against a PUT")
    if o["iv"]:
        parts.append(f"ATM IV {o['iv']:.1f}")
    return f"Options ({o['expiry']}): " + " | ".join(parts) + "\n" + "".join(f"  {f}\n" for f in flags)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, 1e-9))


def side_checks(df: pd.DataFrame, fut, d: int, lag: int = 0):
    """Checklist for one side. d=+1 breakout up (CALL), d=-1 breakdown (PUT).
    Each check is (True/False/None, text); None = informational only."""
    n = len(df)
    t = n - 1 - HOLD_MIN - lag                # index of the breakout candle
    if t <= OPENING_RANGE_MIN:
        return None, [(False, "Waiting for enough candles after the opening range")]
    word = "above" if d > 0 else "below"
    last, trig = df.iloc[-1], df.iloc[t]
    before = df.iloc[:t]
    orng = df.iloc[:OPENING_RANGE_MIN]
    look = before.iloc[-LEVEL_WINDOW:]
    level = (max(orng["High"].max(), look["High"].max()) if d > 0
             else min(orng["Low"].min(), look["Low"].min()))
    checks = []

    # 1. Breakout beyond the level by a buffer
    past = d * (trig.Close - level)
    checks.append((past >= level * BREAKOUT_BUFFER_PCT / 100,
                   f"Breakout candle closed {past:+.0f} pts past level {level:,.0f}"))

    # 2. Strong candle
    rng_t = trig.High - trig.Low
    avg_rng = (before["High"] - before["Low"]).iloc[-20:].mean()
    body = d * (trig.Close - trig.Open)
    if rng_t > 0 and avg_rng > 0:
        checks.append((body >= BODY_FRAC * rng_t and rng_t >= RANGE_MULT * avg_rng,
                       f"Strong candle: body {body / rng_t:.0%} of range, size {rng_t / avg_rng:.1f}x avg"))
    else:
        checks.append((False, "Breakout candle has no range"))

    # 3. Futures volume on the breakout candle
    has_fut = fut is not None and len(fut) >= 25 and fut["Volume"].sum() > 0
    if has_fut and trig.name in fut.index:
        pos = fut.index.get_loc(trig.name)
        base = fut["Volume"].iloc[max(0, pos - 20):pos].mean()
        if base > 0:
            ratio = fut["Volume"].iloc[pos] / base
            checks.append((ratio >= VOL_BREAKOUT_RATIO, f"Breakout volume {ratio:.1f}x average (futures)"))
        else:
            checks.append((None, "Volume unverified (no base volume)"))
    else:
        checks.append((None, "Volume unverified (no futures data)"))

    # 4. Hold beyond the level, 5. not chasing
    held = (all(d * (x - level) > 0 for x in df["Close"].iloc[t + 1:])
            and d * (last.Close - (trig.Open + trig.Close) / 2) > 0)
    checks.append((held, f"Held {word} the level for {HOLD_MIN + lag} candles"))
    ext = d * (last.Close - level)
    checks.append((ext <= level * MAX_EXTENSION_PCT / 100, f"Not chasing ({ext:+.0f} pts past level now)"))

    # 6. Trend, VWAP, RSI
    ema9 = df["Close"].ewm(span=9, adjust=False).mean().iloc[-1]
    ema21 = df["Close"].ewm(span=21, adjust=False).mean().iloc[-1]
    checks.append((d * (ema9 - ema21) > 0, f"EMA9 {word} EMA21"))
    if has_fut:
        tp = (fut["High"] + fut["Low"] + fut["Close"]) / 3
        vwap = (tp * fut["Volume"]).cumsum() / fut["Volume"].cumsum()
        checks.append((d * (fut["Close"].iloc[-1] - vwap.iloc[-1]) > 0, f"Futures {word} VWAP"))
    r = rsi(df["Close"]).iloc[-1]
    lo, hi = (55, 88) if d > 0 else (12, 45)
    checks.append((lo <= r <= hi, f"RSI {r:.0f} (healthy zone {lo}-{hi})"))

    # 7. Market is moving
    win = df.iloc[-RANGE_WINDOW:]
    rp = (win["High"].max() - win["Low"].min()) / last.Close * 100
    checks.append((rp >= MIN_RANGE_PCT, f"30-min range {rp:.2f}% (min {MIN_RANGE_PCT}%)"))
    return trig.name, [(None if c is None else bool(c), txt) for c, txt in checks]


def analyze(df: pd.DataFrame, fut: pd.DataFrame = None):
    """Returns (signal, score, reasons, breakout_candle_time).
    Looks for a breakout candle HOLD_MIN..HOLD_MIN+LATE_GRACE candles ago, so a skipped
    minute can't make us miss an alert."""
    if len(df) < 25:
        return WAIT, 0, ["Not enough candles yet (need ~25)."], None
    fmt = lambda checks: [("✓" if c is True else "✗" if c is False else "•") + " " + txt
                          for c, txt in checks]
    passes = lambda cs: sum(1 for ok, _ in cs if ok is True)
    sides = {1: side_checks(df, fut, 1), -1: side_checks(df, fut, -1)}   # lag 0, used for display
    for lag in range(LATE_GRACE + 1):
        for d in (1, -1):
            t_time, checks = sides[d] if lag == 0 else side_checks(df, fut, d, lag)
            if t_time is not None and all(c is not False for c, _ in checks):
                return (CALL if d > 0 else PUT), d * passes(checks), fmt(checks), t_time
    d = 1 if passes(sides[1][1]) >= passes(sides[-1][1]) else -1
    t_time, checks = sides[d]
    return WAIT, d * passes(checks), fmt(checks), t_time


def observation(df: pd.DataFrame, fut) -> str:
    """One-line price + volume summary of the last closed 1-minute candle."""
    k = df.iloc[-1]
    move = k.Close - k.Open
    line = (f"{df.index[-1]:%H:%M} Nifty O {k.Open:,.1f} H {k.High:,.1f} L {k.Low:,.1f} "
            f"C {k.Close:,.1f} ({move:+.1f}, {'UP' if move > 0 else 'DOWN' if move < 0 else 'FLAT'})")
    if fut is not None and len(fut) >= 22:
        v = float(fut["Volume"].iloc[-1])
        base = fut["Volume"].iloc[-21:-1].mean()
        ratio = f"{v / base:.1f}x avg" if base > 0 else "n/a"
        late = f" [futures candle {fut.index[-1]:%H:%M}]" if fut.index[-1] != df.index[-1] else ""
        line += f"\nFutures volume {v:,.0f} ({ratio}){late}"
    else:
        line += "\nFutures volume n/a"
    fl = flow_note(df, fut).strip()
    return line + ("\n" + fl if fl else "")


def early_signal(df: pd.DataFrame, fut=None) -> str:
    """Simple, weaker score-based signal (EMA, futures VWAP, opening range, RSI, engulfing).
    Used for the TREND status and the end-of-day count, never sent as an entry alert."""
    if len(df) < 25:
        return WAIT
    c = df["Close"]
    ema9, ema21 = c.ewm(span=9, adjust=False).mean(), c.ewm(span=21, adjust=False).mean()
    last, prev = df.iloc[-1], df.iloc[-2]
    score = 1 if ema9.iloc[-1] > ema21.iloc[-1] else -1
    if ema9.iloc[-1] > ema21.iloc[-1] and ema9.iloc[-2] <= ema21.iloc[-2]:
        score += 1
    elif ema9.iloc[-1] < ema21.iloc[-1] and ema9.iloc[-2] >= ema21.iloc[-2]:
        score -= 1
    if fut is not None and len(fut) >= 25 and fut["Volume"].sum() > 0:
        tp = (fut["High"] + fut["Low"] + fut["Close"]) / 3
        vwap = (tp * fut["Volume"]).cumsum() / fut["Volume"].cumsum()
        score += 1 if fut["Close"].iloc[-1] > vwap.iloc[-1] else -1
    orng = df.iloc[:OPENING_RANGE_MIN]
    if len(df) > OPENING_RANGE_MIN:
        if last.Close > orng["High"].max():
            score += 1
        elif last.Close < orng["Low"].min():
            score -= 1
    r = rsi(c).iloc[-1]
    score += 1 if 55 < r < 70 else -1 if 30 < r < 45 else 0
    pb, lb = prev.Close - prev.Open, last.Close - last.Open
    if pb < 0 < lb and last.Close > prev.Open and last.Open < prev.Close:
        score += 1
    elif pb > 0 > lb and last.Close < prev.Open and last.Open > prev.Close:
        score -= 1
    return CALL if score >= 3 else PUT if score <= -3 else WAIT


# ------------------------------------------------------------------ extra signal types
def indicators(df: pd.DataFrame, fut) -> pd.DataFrame:
    """EMA 9/21/50, RSI, futures volume ratio and futures-vs-VWAP gap, one row per candle."""
    c = df["Close"]
    ind = pd.DataFrame(index=df.index)
    ind["ema9"] = c.ewm(span=9, adjust=False).mean()
    ind["ema21"] = c.ewm(span=21, adjust=False).mean()
    ind["ema50"] = c.ewm(span=50, adjust=False).mean()
    ind["rsi"] = rsi(c)
    ind["vol_x"] = float("nan")
    ind["vwap_gap"] = float("nan")
    if fut is not None and len(fut) >= 5 and fut["Volume"].sum() > 0:
        f = fut.reindex(df.index)
        base = f["Volume"].shift(1).rolling(20, min_periods=5).mean()
        ind["vol_x"] = f["Volume"] / base
        vol = f["Volume"].fillna(0)
        tp = (f["High"] + f["Low"] + f["Close"]) / 3
        vwap = (tp * vol).cumsum() / vol.cumsum().replace(0, float("nan"))
        ind["vwap_gap"] = f["Close"] - vwap
    return ind


def net_volume(fut):
    """Signed futures volume per candle: + if the futures candle closed above the previous close (buying),
    - if below (selling), 0 if unchanged or first. Returns None when there is no futures volume."""
    if fut is None or not len(fut) or fut["Volume"].sum() <= 0:
        return None
    return np.sign(fut["Close"].diff()).fillna(0) * fut["Volume"]


def flow_stats(df, fut, window=None):
    """(net, total, pct) over the last `window` futures candles up to the latest index candle, or None."""
    window = window or FLOW_WINDOW
    nv = net_volume(fut)
    if nv is None or not len(df):
        return None
    nv = nv[nv.index <= df.index[-1]].iloc[-window:]
    tot = float(fut["Volume"][fut.index <= df.index[-1]].iloc[-window:].sum())
    if not len(nv) or tot <= 0:
        return None
    net = float(nv.sum())
    return net, tot, net / tot * 100


def combo_state(df, ind, back=0):
    """Two-letter key: volume (U up, D down) then price (u up, f flat, d down) over the last COMBO_WINDOW candles
    (ending `back` candles ago). None when volume is normal or there is not enough data."""
    w = COMBO_WINDOW
    end = len(df) - back
    if end < w + 1 or "vol_x" not in ind or len(ind) < end:
        return None
    v = ind["vol_x"].iloc[end - w:end].mean()
    c1, c0 = float(df["Close"].iloc[end - 1]), float(df["Close"].iloc[end - 1 - w])
    if pd.isna(v) or c0 <= 0:
        return None
    vs = "U" if v >= COMBO_VOL_UP_X else "D" if v <= COMBO_VOL_DOWN_X else None
    if vs is None:
        return None
    pct = (c1 - c0) / c0 * 100
    return vs + ("u" if pct > COMBO_FLAT_PCT else "d" if pct < -COMBO_FLAT_PCT else "f")


def combo_note(df, ind, signal=None) -> str:
    """One line (ending in a newline) with the current volume + price combination; empty when volume is normal."""
    k = combo_state(df, ind)
    if not k:
        return ""
    name, meaning, bias = COMBOS[k]
    line = f"{name}: {meaning}"
    if (signal == CALL and bias < 0) or (signal == PUT and bias > 0):
        line += "  <- against this signal"
    return line + "\n"


def flow_note(df, fut, signal=None) -> str:
    """One line (ending in a newline) with the recent net volume; empty if there is no futures volume.
    With a signal, it also says when the flow points the other way."""
    s = flow_stats(df, fut)
    if s is None:
        return ""
    net, tot, pct = s
    word = "buyers dominating" if pct >= FLOW_DOM_PCT else "sellers dominating" if pct <= -FLOW_DOM_PCT else "balanced"
    line = f"Net volume last {FLOW_WINDOW}m: {net / 1000:+,.0f}k ({pct:+.0f}% of {tot / 1000:,.0f}k), {word}"
    if (signal == CALL and pct <= -FLOW_DOM_PCT) or (signal == PUT and pct >= FLOW_DOM_PCT):
        line += "  <- flow is against this signal"
    return line + "\n"


def flow_eod_lines(d, fut):
    """End-of-day: net volume by hour next to the price move, and high-volume candles at an RSI extreme."""
    nv = net_volume(fut)
    if nv is None:
        return []
    out = ["NET VOLUME by hour (futures; up candle = buying, down candle = selling):"]
    px = d["Close"].diff().fillna(d["Close"] - d["Open"])
    for h in sorted(set(nv.index.hour)):
        m = nv[nv.index.hour == h]
        tot = float(fut["Volume"][fut.index.hour == h].sum())
        if tot > 0:
            out.append(f"{h:02d}:00  net {m.sum() / 1000:+,.0f}k ({m.sum() / tot * 100:+.0f}%) of {tot / 1000:,.0f}k"
                       f" | price {px[px.index.hour == h].sum():+.0f}")
    day_tot = float(fut["Volume"].sum())
    out.append(f"Day: net {nv.sum() / 1000:+,.0f}k ({nv.sum() / day_tot * 100:+.0f}%) of {day_tot / 1000:,.0f}k")
    ind = indicators(d, fut)
    hv = ind[ind["vol_x"] >= FLOW_HV_X]
    ex = hv[(hv["rsi"] <= FLOW_RSI_LOW) | (hv["rsi"] >= FLOW_RSI_HIGH)]
    out.append(f"High-volume candles ({FLOW_HV_X:g}x+): {len(hv)}. At an RSI extreme "
               f"(<= {FLOW_RSI_LOW} or >= {FLOW_RSI_HIGH}, possible exhaustion): "
               + (", ".join(f"{t:%H:%M} (RSI {r.rsi:.0f}, {r.vol_x:.1f}x)" for t, r in ex.head(8).iterrows())
                  + (f" +{len(ex) - 8} more" if len(ex) > 8 else "") if len(ex) else "none"))
    return out + [""]


def level_at(df: pd.DataFrame, j: int, d: int):
    """Break level before candle j: opening-range extreme or last-30-candle extreme."""
    if j <= OPENING_RANGE_MIN:
        return None
    orng, look = df.iloc[:OPENING_RANGE_MIN], df.iloc[:j].iloc[-LEVEL_WINDOW:]
    return (max(orng["High"].max(), look["High"].max()) if d > 0
            else min(orng["Low"].min(), look["Low"].min()))


def detect_fake(df, ind):
    """FAKE BREAKOUT: a high-volume break that closes back inside the range."""
    n, last = len(df), df.iloc[-1]
    for k in range(1, FAKE_WINDOW + 1):
        j = n - 1 - k
        for d in (1, -1):
            lvl = level_at(df, j, d)
            if lvl is None:
                continue
            cand, vx = df.iloc[j], ind["vol_x"].iloc[j]
            broke = d * (cand.Close - lvl) >= lvl * BREAKOUT_BUFFER_PCT / 100
            volok = pd.isna(vx) or vx >= FAKE_VOL_RATIO
            margin = lvl * FAKE_MARGIN_PCT / 100
            back = d * (last.Close - lvl) < -margin and d * (last.Close - last.Open) < 0
            first_back = all(d * (x - lvl) >= -margin for x in df["Close"].iloc[j + 1:n - 1])
            if broke and volok and back and first_back:
                vtxt = "volume unverified" if pd.isna(vx) else f"{vx:.1f}x volume"
                return {"signal": PUT if d > 0 else CALL, "key": df.index[j],
                        "why": [f"Candle {df.index[j]:%H:%M} broke {'above' if d > 0 else 'below'} {lvl:,.1f} "
                                f"({vtxt}) but price closed back inside at {last.Close:,.1f}"]}
    return None


def detect_pullback(df, ind):
    """PULLBACK: in a trend, a bounce against it fails (rejection candle)."""
    n, last, r = len(df), df.iloc[-1], ind.iloc[-1]
    hm = (df.index[-1].hour, df.index[-1].minute)
    if n < 30 or hm >= PULL_NO_ENTRY_AFTER or (ACCURACY_MODE and hm < PULL_NO_ENTRY_BEFORE):
        return None
    win = df.iloc[-RANGE_WINDOW:]
    if (win["High"].max() - win["Low"].min()) / last.Close * 100 < PULL_MIN_RANGE_PCT:
        return None  # market is not moving enough; pullbacks in a flat range mostly lose
    w, wi = df.iloc[-1 - PULL_BOUNCE_WINDOW:-1], ind.iloc[-1 - PULL_BOUNCE_WINDOW:-1]
    for d in (-1, 1):
        stacked = d * (r.ema9 - r.ema21) > 0 and d * (r.ema21 - r.ema50) > 0
        vw_ok = pd.isna(r.vwap_gap) or d * r.vwap_gap > 0
        bounced = ((-d) * (w["Close"] - wi["ema21"]) > 0).any()
        rej = (d * (last.Close - r.ema9) > 0 and d * (last.Close - r.ema21) > 0
               and d * (last.Close - last.Open) > 0 and (pd.isna(r.vol_x) or r.vol_x >= 1.0))
        if stacked and vw_ok and bounced and rej:
            return {"signal": CALL if d > 0 else PUT, "key": df.index[-1],
                    "why": [f"{'Up' if d > 0 else 'Down'}trend (EMA 9/21/50 stacked, futures "
                            f"{'above' if d > 0 else 'below'} VWAP)",
                            f"Bounce against the trend failed: closed {last.Close:,.1f}, "
                            f"{'above' if d > 0 else 'below'} EMA9 {r.ema9:,.1f} and EMA21 {r.ema21:,.1f}"]}
    return None


def detect_reverse(df, ind):
    """TREND REVERSE: fresh EMA cross + VWAP flip + break of recent structure."""
    n, last, r = len(df), df.iloc[-1], ind.iloc[-1]
    if n < 35:
        return None
    for d in (1, -1):
        gaps = [d * (ind["ema9"].iloc[k] - ind["ema21"].iloc[k]) for k in range(-1 - REV_CROSS_WINDOW, -1)]
        fresh = d * (r.ema9 - r.ema21) > 0 and any(g <= 0 for g in gaps)
        vg = ind["vwap_gap"].iloc[-1 - REV_VWAP_WINDOW:-1].dropna()
        vwap_ok = pd.isna(r.vwap_gap) or (d * r.vwap_gap > 0 and any(d * g < 0 for g in vg))
        prior = df.iloc[-1 - REV_STRUCT_WINDOW:-1]
        edge = prior["High"].max() if d > 0 else prior["Low"].min()
        struct = d * (last.Close - edge) > 0
        volok = pd.isna(r.vol_x) or r.vol_x >= REV_VOL_RATIO
        rsiok = (50 < r.rsi < 80) if d > 0 else (20 < r.rsi < 50)
        if fresh and vwap_ok and struct and volok and rsiok:
            return {"signal": CALL if d > 0 else PUT, "key": df.index[-1],
                    "why": [f"EMA9 crossed {'above' if d > 0 else 'below'} EMA21 within {REV_CROSS_WINDOW} candles",
                            f"Futures flipped {'above' if d > 0 else 'below'} VWAP",
                            f"Closed {last.Close:,.1f}, past the last {REV_STRUCT_WINDOW}-candle "
                            f"{'high' if d > 0 else 'low'} {edge:,.1f}"]}
    return None


def detect_indication(df, ind):
    """INDICATION: early reversal read on 8-24 candles. v7: summary only, not sent live."""
    n = len(df)
    if n < IND_MIN_CANDLES or n >= FULL_CANDLES:
        return None
    last, r = df.iloc[-1], ind.iloc[-1]
    if pd.isna(r.vwap_gap):
        return None  # needs futures data
    for d in (1, -1):
        ext_idx = int(df["Low"].values.argmin() if d > 0 else df["High"].values.argmax())
        near_ext = (n - 1 - ext_idx) <= IND_EXT_WINDOW
        gaps = ind["vwap_gap"].iloc[max(0, n - 1 - IND_FLIP_WINDOW):n - 1]
        flip = d * r.vwap_gap > 0 and (d * gaps < 0).any()
        body = d * (last.Close - last.Open) > 0
        prior = df.iloc[n - 3:n - 1]
        beyond = d * (last.Close - (prior["High"].max() if d > 0 else prior["Low"].min())) > 0
        if near_ext and flip and body and beyond:
            recent = df.iloc[-IND_INVALID_WINDOW:]
            inval = float(recent["Low"].min() if d > 0 else recent["High"].max())
            ext = df["Low"].min() if d > 0 else df["High"].max()
            return {"signal": CALL if d > 0 else PUT, "key": CALL if d > 0 else PUT,
                    "entry": float(last.Close), "inval": inval, "d": d, "candles": n,
                    "why": [f"Session {'low' if d > 0 else 'high'} {ext:,.1f} made {n - 1 - ext_idx} candles ago",
                            f"Futures flipped {'above' if d > 0 else 'below'} VWAP",
                            f"Closed {last.Close:,.1f}, past the last 2 candles' {'high' if d > 0 else 'low'}",
                            f"Based on only {n} candles. Confirmation comes at {FULL_CANDLES} candles."]}
    return None


def observe(df, ind):
    """A developing setup worth watching. Returns (key, text) or None. Not a trade signal."""
    n, last, r = len(df), df.iloc[-1], ind.iloc[-1]
    if n < 30:
        return None
    if not pd.isna(r.vol_x) and r.vol_x >= WATCH_VOL_RATIO:
        return "volume", f"High-volume candle ({r.vol_x:.1f}x) at {last.Close:,.1f}. Checking for follow-through."
    for d in (-1, 1):
        trend = d * (r.ema9 - r.ema21) > 0 and (pd.isna(r.vwap_gap) or d * r.vwap_gap > 0)
        lvl = level_at(df, n, d)
        if not trend or lvl is None:
            continue
        dist = d * (last.Close - lvl)
        if -WATCH_PCT / 100 * lvl <= dist < 0:
            side = "upside" if d > 0 else "downside"
            return f"near{d}", (f"Nifty {last.Close:,.1f} is {abs(dist):.0f} pts from the {side} break level "
                                f"{lvl:,.1f} ({'up' if d > 0 else 'down'}trend).")
    return None


def trend_direction_held(df, fut, hold=None):
    """CALL / PUT if the weak-trend score has pointed the same way for `hold` candles in a row, else None."""
    hold = hold or TREND_HOLD_MIN
    n = len(df)
    if n < FULL_CANDLES + hold - 1:
        return None
    first = None
    for k in range(hold):
        d = df.iloc[: n - k]
        f = fut[fut.index <= d.index[-1]] if fut is not None else None
        e = early_signal(d, f)
        if e == WAIT:
            return None
        if first is None:
            first = e
        elif e != first:
            return None
    return first


def trend_events(df, fut):
    """Replay: every time the held trend direction changes. Returns [(time, 'UP' or 'DOWN')]."""
    dirs = []
    for i in range(len(df)):
        d = df.iloc[: i + 1]
        f = fut[fut.index <= d.index[-1]] if fut is not None else None
        dirs.append(early_signal(d, f) if i + 1 >= FULL_CANDLES else WAIT)
    events, reported = [], None
    for i in range(TREND_HOLD_MIN - 1, len(dirs)):
        w = dirs[i - TREND_HOLD_MIN + 1: i + 1]
        if w[0] != WAIT and all(x == w[0] for x in w) and w[0] != reported:
            reported = w[0]
            events.append((df.index[i], "UP" if w[0] == CALL else "DOWN"))
    return events


# Confidence labels use the 12-day replay (8-26 Oct 2026): win rate = 60-minute result in the signal's direction.
KINDS = (("INDICATION", "SUMMARY only (3 of 10 won)"),
         ("BREAKOUT", "HIGH confidence (about 7 in 10 won, 28 seen in 27 days, chop-filtered)"),
         ("FAKE BREAKOUT", "LOWER confidence (only 5 seen, 2 won)"),
         ("PULLBACK", "MEDIUM confidence (about 7 in 10 won, 10:00-14:30, chop-filtered)"),
         ("TREND REVERSE", "SUMMARY only (33 seen, 36% won, -286 pts)"),
         ("OPENING BREAK", "MEDIUM confidence (7 of 10 won with the stop)"),
         ("SWING", "MEDIUM confidence (89 seen, about 44% won, +1,121 pts held to the next swing; late by design)"))


def is_aligned(df, kind, signal) -> bool:
    """True if the signal agrees with price vs the day's open at this moment (exempt kinds always agree)."""
    if kind not in ALIGN_KINDS:
        return True
    d = 1 if signal == CALL else -1
    return d * (df["Close"].iloc[-1] - df["Open"].iloc[0]) > 0


def aligned_ok(df, kind, signal) -> bool:
    """Should this signal be sent? Only 'block' mode ever says no; 'tag' mode sends and marks it."""
    return ALIGN_MODE != "block" or is_aligned(df, kind, signal)


def time_ok(df, kind) -> bool:
    """v7 time rules: nothing new from NO_NEW_AFTER, and no BREAKOUT from BREAKOUT_LAST."""
    t = df.index[-1]
    hm = (t.hour, t.minute)
    if hm >= NO_NEW_AFTER:
        return False
    return not (kind == "BREAKOUT" and hm >= BREAKOUT_LAST)


def choppy(ind):
    """(is_choppy, vwap_crosses, ema_crosses) over the last CHOP_WINDOW candles."""
    w = ind.iloc[-CHOP_WINDOW:]

    def crosses(x):
        sg = np.sign(x.dropna())
        sg = sg[sg != 0]
        return int((sg != sg.shift()).sum() - 1) if len(sg) else 0
    vc, ec = crosses(w["vwap_gap"]), crosses(w["ema9"] - w["ema21"])
    return vc >= CHOP_MAX_CROSSES or ec >= CHOP_MAX_CROSSES, vc, ec


def accuracy_ok(df, ind, s, st) -> bool:
    """v8.3: opposite-direction flips within FLIP_COOLDOWN_MIN are blocked, and chop blocks CHOP_KINDS."""
    if not ACCURACY_MODE or s["kind"] not in LIVE_KINDS:
        return True
    if (st.last_dir is not None and s["signal"] != st.last_dir
            and (df.index[-1] - st.last_dir_t).total_seconds() < FLIP_COOLDOWN_MIN * 60):
        return False
    return not (s["kind"] in CHOP_KINDS and choppy(ind)[0])


def mark_dir(st, df, signal) -> None:
    st.last_dir, st.last_dir_t = signal, df.index[-1]


def detect_opening_break(df):
    """OPENING BREAK: the first close beyond the 09:15-09:29 range (09:30 to OPENING_BREAK_UNTIL)."""
    n = len(df)
    if n <= OPENING_RANGE_MIN:
        return None
    t = df.index[-1]
    if (t.hour, t.minute) >= OPENING_BREAK_UNTIL:
        return None
    hi, lo = df["High"].iloc[:OPENING_RANGE_MIN].max(), df["Low"].iloc[:OPENING_RANGE_MIN].min()
    closes = df["Close"].iloc[OPENING_RANGE_MIN:]
    first = next((k for k, c in enumerate(closes) if c > hi or c < lo), None)
    if first is None or first != len(closes) - 1:
        return None  # not the first close beyond the range, or this candle is not it
    c = float(closes.iloc[-1])
    up = c > hi
    mid = float((hi + lo) / 2)
    if ACCURACY_MODE and abs(c - mid) > OB_MAX_RISK_PTS:
        return None  # v8.3: range too wide (stop too far), skip
    return {"signal": CALL if up else PUT, "key": f"{t:%Y-%m-%d}", "entry": c, "stop": mid, "d": 1 if up else -1,
            "why": [f"First close {'above' if up else 'below'} the opening range {lo:,.1f} to {hi:,.1f}",
                    f"Stop idea: the range middle {mid:,.1f} ({abs(c - mid):.0f} pts risk). "
                    f"Exit idea: {OB_EXIT[0]}:{OB_EXIT[1]:02d}"]}


def swing_replay(df):
    """All SWING signals so far today (same rules live and in the end-of-day replay).
    A swing bottom is confirmed when price closes SWING_PTS above the lowest low since the last signal (and that
    low is at least SWING_MIN_BARS candles old) -> CALL. A swing top is the mirror image -> PUT.
    Signals must alternate (CALL, PUT, CALL ...). Returns a list of dicts."""
    if len(df) < 10:
        return []
    mins = df.index.hour * 60 + df.index.minute
    sel = np.flatnonzero((mins >= SWING_START[0] * 60 + SWING_START[1]) & (mins <= SWING_EXIT[0] * 60 + SWING_EXIT[1]))
    if len(sel) < 2:
        return []
    hi, lo, cl = df["High"].values, df["Low"].values, df["Close"].values
    ema = df["Close"].ewm(span=9, adjust=False).mean().values
    out, last_d, last_i = [], 0, None
    h = l = sel[0]
    for i in sel[1:]:
        if hi[i] > hi[h]:
            h = i
        if lo[i] < lo[l]:
            l = i
        c, d, ext_i = cl[i], 0, None
        if last_d != 1 and c - lo[l] >= SWING_PTS and i - l >= SWING_MIN_BARS:
            d, ext_i = 1, l
            if SWING_EMA_CONFIRM and c < ema[i]:
                d = 0
        elif last_d != -1 and hi[h] - c >= SWING_PTS and i - h >= SWING_MIN_BARS:
            d, ext_i = -1, h
            if SWING_EMA_CONFIRM and c > ema[i]:
                d = 0
        if d and (last_i is None or mins[i] - mins[last_i] >= SWING_COOLDOWN_MIN):
            ext = float(lo[ext_i] if d > 0 else hi[ext_i])
            out.append({"i": int(i), "time": df.index[i], "d": d, "signal": CALL if d > 0 else PUT,
                        "entry": float(c), "extreme": ext, "ext_time": df.index[ext_i],
                        "stop": ext - d * SWING_STOP_BUFFER})
            last_d, last_i = d, i
            h = l = i
    return out


def detect_swing(df):
    """SWING: a top or bottom just confirmed on the latest candle (v8)."""
    sigs = swing_replay(df)
    if not sigs or sigs[-1]["i"] != len(df) - 1:
        return None
    s = sigs[-1]
    d, kind = s["d"], "bottom" if s["d"] > 0 else "top"
    return {"signal": s["signal"], "key": f"{s['time']:%H:%M}", "entry": s["entry"], "stop": s["stop"], "d": d,
            "why": [f"Swing {kind} {s['extreme']:,.1f} at {s['ext_time']:%H:%M}, price has reversed "
                    f"{abs(s['entry'] - s['extreme']):.0f} pts ({SWING_PTS}-pt swing)",
                    f"Stop idea: {s['stop']:,.1f} ({abs(s['entry'] - s['stop']):.0f} pts risk). Exit idea: the next "
                    f"swing signal, or {SWING_EXIT[0]}:{SWING_EXIT[1]:02d}",
                    "Late by design (confirmed after the reversal). About 4 in 10 swings win, a few big runs pay."]}


def swing_outcomes(d):
    """End-of-day result of every swing trade: held to the next swing signal (or SWING_EXIT), stop at the extreme +/- buffer.
    Honours the v7 time rule (no new swing signal at or after NO_NEW_AFTER). Returns (lines, total_pts, trades)."""
    sigs = [s for s in swing_replay(d) if (s["time"].hour, s["time"].minute) < NO_NEW_AFTER]
    lines, total = [], 0.0
    exit_i = int(np.flatnonzero(d.index.hour * 60 + d.index.minute <= SWING_EXIT[0] * 60 + SWING_EXIT[1])[-1]) if len(d) else 0
    for k, s in enumerate(sigs):
        end = sigs[k + 1]["i"] if k + 1 < len(sigs) else exit_i
        res, how = None, ("next swing " + f"{sigs[k + 1]['time']:%H:%M}") if k + 1 < len(sigs) else f"{SWING_EXIT[0]}:{SWING_EXIT[1]:02d}"
        for j in range(s["i"] + 1, end + 1):
            if (s["d"] > 0 and d["Low"].iloc[j] <= s["stop"]) or (s["d"] < 0 and d["High"].iloc[j] >= s["stop"]):
                res, how = s["d"] * (s["stop"] - s["entry"]), f"stopped {d.index[j]:%H:%M}"
                break
        if res is None:
            res = s["d"] * (d["Close"].iloc[end] - s["entry"])
        total += res
        lines.append(f"{s['time']:%H:%M} {'CALL' if s['d'] > 0 else 'PUT'} SWING @{s['entry']:,.1f} | stop {s['stop']:,.0f} | "
                     f"result {res:+.0f} ({how})")
    return lines, total, len(sigs)


def opening_break_outcome(d, t, sig, i):
    """Result of an opening break held to the exit time, with a stop at the range middle."""
    sign = 1 if sig == CALL else -1
    entry = d["Close"].iloc[i]
    hi, lo = d["High"].iloc[:OPENING_RANGE_MIN].max(), d["Low"].iloc[:OPENING_RANGE_MIN].min()
    stop, res, how = (hi + lo) / 2, None, f"{OB_EXIT[0]}:{OB_EXIT[1]:02d}"
    for j in range(i + 1, len(d)):
        ts = d.index[j]
        if (sign > 0 and d["Low"].iloc[j] <= stop) or (sign < 0 and d["High"].iloc[j] >= stop):
            res, how = sign * (stop - entry), f"stopped {ts:%H:%M}"
            break
        if (ts.hour, ts.minute) >= OB_EXIT:
            res = sign * (d["Close"].iloc[j] - entry)
            break
    if res is None:
        res, how = sign * (d["Close"].iloc[-1] - entry), "close"
    return (f"{t:%H:%M} {'CALL' if sign > 0 else 'PUT'} OPENING BREAK @{entry:,.1f} | stop {stop:,.0f} | "
            f"result {res:+.0f} ({how})")


def opening_read_message(df, prev_close=None) -> str:
    o, c = df["Open"].iloc[0], df["Close"].iloc[OPENING_RANGE_MIN - 1]
    hi, lo = df["High"].iloc[:OPENING_RANGE_MIN].max(), df["Low"].iloc[:OPENING_RANGE_MIN].min()
    mv = c - o
    bias = "UP" if mv >= 15 else "DOWN" if mv <= -15 else "FLAT (under 15 pts)"
    lines = ["OPENING READ (first 15 minutes)"]
    if prev_close:
        lines.append(f"Gap: {o - prev_close:+,.0f} pts (open {o:,.1f} vs yesterday's close {prev_close:,.1f})")
    lines += [f"First 15 min: {mv:+,.0f} pts ({o:,.1f} to {c:,.1f})",
              f"Range: {lo:,.1f} to {hi:,.1f}",
              f"Bias: {bias}",
              f"Break levels: a close above {hi:,.1f} = UP break, below {lo:,.1f} = DOWN break "
              f"(an OPENING BREAK alert is sent if it happens before 10:00).",
              "Past 12 days: the first-15-minute direction matched the day on 9 of 12 days, and the day's "
              "high or low was already set by 09:48 on all 12. Small sample, context only.",
              "", "Not financial advice."]
    return "\n".join(lines)


def fetch_prev_close():
    """Yesterday's Nifty close from the daily candles (None if it cannot be fetched)."""
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    today = datetime.now(IST).date()
    url = (f"https://api.upstox.com/v3/historical-candle/{quote(INDEX_KEY, safe='')}/days/1/"
           f"{(today - timedelta(days=1)).isoformat()}/{(today - timedelta(days=10)).isoformat()}")
    r = requests.get(url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    candles = r.json()["data"]["candles"]   # [ts, o, h, l, c, ...]
    if not candles:
        return None
    return float(max(candles, key=lambda c: c[0])[4])


def scan_candle(df, fut):
    """All signals on the latest candle. Returns (list of signal dicts, indicators)."""
    ind = indicators(df, fut)
    out = []
    ind_sig = detect_indication(df, ind)
    if ind_sig:
        out.append({"kind": "INDICATION", **ind_sig})
    sig, _, why, trig = analyze(df, fut)
    if sig != WAIT:
        out.append({"kind": "BREAKOUT", "signal": sig, "key": trig, "why": why})
    ob = detect_opening_break(df)
    if ob:
        out.append({"kind": "OPENING BREAK", **ob})
    sw = detect_swing(df)
    if sw:
        out.append({"kind": "SWING", **sw})
    for kind, fn in (("FAKE BREAKOUT", detect_fake), ("PULLBACK", detect_pullback),
                     ("TREND REVERSE", detect_reverse)):
        r = fn(df, ind)
        if r:
            out.append({"kind": kind, **r})
    return out, ind


class State:
    """Per-day memory: cooldowns, de-duplication, the current WAIT note and the open OPENING BREAK idea."""

    def __init__(self):
        self.last_time, self.last_key = {}, {}
        self.watch, self.watch_since, self.last_watch = None, None, None
        self.indication = None
        self.trend_reported = None
        self.prev_close = None
        self.opening_read_done = False
        self.ob = None          # {"d", "entry", "stop", "time"} while an opening break idea is open
        self.swing = None       # same shape, while a swing idea is open (v8)
        self.last_dir, self.last_dir_t = None, None   # v8.3: direction and candle time of the last live alert
        self.combo_last, self.combo_sent_t = None, None   # v8.7
        self.last_vol_alert = None   # v8.6: candle time of the last volume alert

    def allow(self, kind, key, now) -> bool:
        if key == self.last_key.get(kind):
            return False
        lt = self.last_time.get(kind)
        cool = IND_COOLDOWN_MIN if kind == "INDICATION" else 0 if kind == "SWING" else COOLDOWN_MIN   # swing has its own cooldown
        if lt is not None and (now - lt).total_seconds() < cool * 60:
            return False
        self.last_key[kind], self.last_time[kind] = key, now
        return True


def process_candle(df, fut, now, st: State) -> list:
    """Everything that happens on a new candle. Returns the Telegram messages to send."""
    msgs, conf = [], dict(KINDS)
    sigs, ind = scan_candle(df, fut)
    price, ts, n = df["Close"].iloc[-1], f"{df.index[-1]:%H:%M}", len(df)
    hm = (df.index[-1].hour, df.index[-1].minute)
    # --- OPENING READ: once, as soon as the first 15 minutes are complete
    if SEND_OPENING_READ and not st.opening_read_done and n >= OPENING_RANGE_MIN:
        st.opening_read_done = True
        msgs.append(opening_read_message(df, st.prev_close))
    # --- an open OPENING BREAK idea: stop touched, or exit time reached
    if st.ob:
        o = st.ob
        side = "CALL" if o["d"] > 0 else "PUT"
        touched = (df["Low"].iloc[-1] <= o["stop"]) if o["d"] > 0 else (df["High"].iloc[-1] >= o["stop"])
        if touched:
            msgs.append(f"OPENING BREAK STOP HIT ({ts})\nNifty touched the stop idea {o['stop']:,.1f}. "
                        f"The {side} idea from {o['time']} (entry {o['entry']:,.1f}) is out.\n\nNot financial advice.")
            st.ob = None
        elif hm >= OB_EXIT:
            msgs.append(f"OPENING BREAK EXIT IDEA ({ts})\nNifty {price:,.2f}, {o['d'] * (price - o['entry']):+.0f} pts "
                        f"from the {side} entry {o['entry']:,.1f} at {o['time']}. Exit idea time reached.\n\n"
                        "Not financial advice.")
            st.ob = None
    # --- an open SWING idea: stop touched, or exit time reached
    if st.swing:
        o = st.swing
        side = "CALL" if o["d"] > 0 else "PUT"
        touched = (df["Low"].iloc[-1] <= o["stop"]) if o["d"] > 0 else (df["High"].iloc[-1] >= o["stop"])
        if touched:
            msgs.append(f"SWING STOP HIT ({ts})\nNifty touched the stop idea {o['stop']:,.1f}. "
                        f"The {side} swing idea from {o['time']} (entry {o['entry']:,.1f}) is out.\n\nNot financial advice.")
            st.swing = None
        elif hm >= SWING_EXIT:
            msgs.append(f"SWING EXIT IDEA ({ts})\nNifty {price:,.2f}, {o['d'] * (price - o['entry']):+.0f} pts "
                        f"from the {side} swing entry {o['entry']:,.1f} at {o['time']}. Exit idea time reached.\n\n"
                        "Not financial advice.")
            st.swing = None
    # --- TREND status: context only, sent when the held direction changes
    if SEND_TREND:
        td = trend_direction_held(df, fut)
        if td and td != st.trend_reported:
            st.trend_reported = td
            up = td == CALL
            msgs.append(f"TREND {'UP' if up else 'DOWN'} (held {TREND_HOLD_MIN} min)\n"
                        f"Nifty {price:,.2f} at {ts} IST\n"
                        f"{flow_note(df, fut)}"
                        f"{combo_note(df, ind, td) if COMBO_IN_ALERTS else ''}"
                        f"The weak-trend score has pointed {'up' if up else 'down'} for {TREND_HOLD_MIN} minutes in a row.\n"
                        f"Bias: favour {'CALL' if up else 'PUT'} setups and be careful with the opposite side.\n"
                        f"Context only, not an entry. Choppy days flip it often.\n\nNot financial advice.")
    # --- VOLUME + PRICE combination changed and held (v8.7)
    if COMBO_ALERT:
        keys = [combo_state(df, ind, back=b) for b in range(COMBO_HOLD)]
        k = keys[0]
        t_now = df.index[-1]
        if (k and k[0] + k[1].upper() in COMBO_SEND and all(x == k for x in keys) and k != st.combo_last
                and (st.combo_sent_t is None or (t_now - st.combo_sent_t).total_seconds() >= COMBO_GAP_MIN * 60)
                and n >= VOL_ALERT_MIN_CANDLES):
            st.combo_last, st.combo_sent_t = k, t_now
            name, meaning, bias = COMBOS[k]
            move = float(df["Close"].iloc[-1] - df["Close"].iloc[-1 - COMBO_WINDOW])
            msgs.append(f"VOLUME + PRICE: {name}\n"
                        f"Nifty {price:,.2f} at {ts} IST, {move:+.1f} pts in {COMBO_WINDOW} candles\n"
                        f"{meaning[0].upper() + meaning[1:]}.\n"
                        f"{flow_note(df, fut)}"
                        "Information only, not a CALL/PUT signal. Wait for a signal alert before acting.\n\n"
                        "Not financial advice.")
    # --- VOLUME SPIKE: information only, sent whenever futures volume jumps (v8.6)
    if VOLUME_ALERT and n >= VOL_ALERT_MIN_CANDLES and len(ind) and pd.notna(ind["vol_x"].iloc[-1]) \
            and ind["vol_x"].iloc[-1] >= VOL_ALERT_X:
        t_now = df.index[-1]
        if st.last_vol_alert is None or (t_now - st.last_vol_alert).total_seconds() >= VOL_ALERT_COOLDOWN_MIN * 60:
            st.last_vol_alert = t_now
            vx = float(ind["vol_x"].iloc[-1])
            move = float(df["Close"].iloc[-1] - df["Open"].iloc[-1])
            word = "BUYING (up candle)" if move > 0 else "SELLING (down candle)" if move < 0 else "no direction (flat candle)"
            fv = float(fut["Volume"][fut.index <= t_now].iloc[-1]) if fut is not None and len(fut) else 0.0
            rs = ind["rsi"].iloc[-1]
            extreme = (" RSI is stretched, so this could be exhaustion, not a fresh move."
                       if pd.notna(rs) and (rs <= FLOW_RSI_LOW or rs >= FLOW_RSI_HIGH) else "")
            msgs.append(f"VOLUME SPIKE: {word}\n"
                        f"Nifty {price:,.2f} at {ts} IST, candle {move:+.1f} pts\n"
                        f"Futures volume {fv / 1000:,.0f}k = {vx:.1f}x the 20-candle average\n"
                        f"{flow_note(df, fut)}"
                        f"{combo_note(df, ind) if COMBO_IN_ALERTS else ''}"
                        f"RSI {rs:.0f}.{extreme}\n"
                        "Information only, not a CALL/PUT signal. Wait for a signal alert before acting.\n\n"
                        "Not financial advice.")
    fired = False
    send = [s for s in sigs
            if time_ok(df, s["kind"]) and aligned_ok(df, s["kind"], s["signal"])
            and s["kind"] in LIVE_KINDS and accuracy_ok(df, ind, s, st)
            and st.allow(s["kind"], s["key"], now)]
    groups = {}
    for s in send:
        groups.setdefault(s["signal"], []).append(s)      # same candle + same direction = one alert
    for signal, grp in groups.items():
        fired = True
        mark_dir(st, df, signal)
        upd = "UPDATE: this confirms the earlier WAIT note.\n" if st.watch else ""
        for s in grp:
            if s["kind"] == "OPENING BREAK":
                st.ob = {"d": s["d"], "entry": s["entry"], "stop": s["stop"], "time": ts}
            if s["kind"] == "SWING":
                if st.swing:      # an open swing idea in the other direction: this signal is its exit idea
                    o = st.swing
                    s["why"].insert(0, f"This flips the open {'CALL' if o['d'] > 0 else 'PUT'} swing idea from {o['time']} "
                                       f"(entry {o['entry']:,.1f}, {o['d'] * (price - o['entry']):+.0f} pts now): exit it.")
                st.swing = {"d": s["d"], "entry": s["entry"], "stop": s["stop"], "time": ts}
        note = ("NOTE: against price vs the day's open (lower odds so far)\n"
                if ALIGN_MODE == "tag" and any(not is_aligned(df, s["kind"], signal) for s in grp) else "")
        if len(grp) == 1:
            s = grp[0]
            head = f"{s['kind']}: {signal}\n{conf[s['kind']]}\n"
            body = "\n".join(s["why"])
        else:
            head = (" + ".join(s["kind"] for s in grp) + f": {signal}\n"
                    + " | ".join(f"{s['kind']}: {conf[s['kind']]}" for s in grp) + "\n")
            body = "\n".join(f"[{s['kind']}] " + " / ".join(x.lstrip("✓•✗ ") for x in s["why"][:3]) for s in grp)
        msgs.append(f"{head}{upd}{note}Nifty {price:,.2f} at {ts} IST\n{flow_note(df, fut, signal)}"
                    f"{combo_note(df, ind, signal) if COMBO_IN_ALERTS else ''}{option_note(price, signal)}{body}\n"
                    "\nNot financial advice. Use your own stop-loss.")
    if fired:
        st.watch = None
        return msgs
    if not SEND_WAIT_NOTES:
        return msgs
    if st.watch and (now - st.watch_since).total_seconds() >= WATCH_TIMEOUT_MIN * 60:
        msgs.append(f"UPDATE: no confirmation for the earlier note ({st.watch.rstrip('.')}). Still WAIT / NO TRADE.")
        st.watch = None
    cool = st.last_watch is None or (now - st.last_watch).total_seconds() >= WATCH_COOLDOWN_MIN * 60
    if st.watch is None and cool:
        o = observe(df, ind)
        if o:
            st.watch, st.watch_since, st.last_watch = o[1], now, now
            msgs.append(f"WAIT - observing ({ts})\n{o[1]}\nNo trade yet. Will update.")
    return msgs


def day_signals(df: pd.DataFrame, fut, skipped=None, late=None):
    """Replay the day candle by candle with the same rules as live.
    Returns a list of (time, kind, signal, candle_index). `skipped` collects signals removed by the alignment
    filter, `late` collects signals removed by the v7 time rules (same tuple format)."""
    st, st_skip, st_late, found = State(), State(), State(), []
    for i in range(IND_MIN_CANDLES - 1, len(df)):
        d = df.iloc[: i + 1]
        f = fut[fut.index <= d.index[-1]] if fut is not None else None
        sigs, ind_d = scan_candle(d, f)
        for s in sigs:
            if not time_ok(d, s["kind"]):
                if late is not None and st_late.allow(s["kind"], s["key"], d.index[-1]):
                    late.append((d.index[-1], s["kind"], s["signal"], i))
                continue
            if not aligned_ok(d, s["kind"], s["signal"]):
                if skipped is not None and st_skip.allow(s["kind"], s["key"], d.index[-1]):
                    skipped.append((d.index[-1], s["kind"], s["signal"], i))
                continue
            if not accuracy_ok(d, ind_d, s, st):
                continue
            if st.allow(s["kind"], s["key"], d.index[-1]):
                found.append((d.index[-1], s["kind"], s["signal"], i))
                if s["kind"] in LIVE_KINDS:
                    mark_dir(st, d, s["signal"])
    return found


def outcome_line(d: pd.DataFrame, t, kind, sig, i):
    sign = 1 if sig == CALL else -1
    entry = d["Close"].iloc[i]
    parts, net60 = [], None
    for mins in (30, 60):
        w = d.iloc[i + 1: i + 1 + mins]
        if w.empty:
            parts.append(f"{mins}m n/a")
            continue
        best = max(0.0, (w["High"].max() - entry) if sign > 0 else (entry - w["Low"].min()))
        worst = max(0.0, (entry - w["Low"].min()) if sign > 0 else (w["High"].max() - entry))
        net = sign * (w["Close"].iloc[-1] - entry)
        parts.append(f"{mins}m +{best:.0f}/-{worst:.0f}/{net:+.0f}")
        net60 = net
    return (f"{t:%H:%M} {'CALL' if sign > 0 else 'PUT'} {kind} @{entry:,.1f} | " + " | ".join(parts)), net60


def eod_summary(df: pd.DataFrame, fut) -> str:
    """End-of-day summary: signals by type with times, and how each one did."""
    d = df.between_time("09:15", "15:30")
    skipped, late = [], []
    found = day_signals(d, fut.between_time("09:15", "15:30") if fut is not None else None, skipped, late)
    first, last = d.iloc[0], d.iloc[-1]
    out = [f"SIGNALS TODAY - Nifty {d.index[-1]:%d %b %Y}",
           f"Open {first.Open:,.1f} | High {d['High'].max():,.1f} | Low {d['Low'].min():,.1f} | "
           f"Close {last.Close:,.1f} ({last.Close - first.Open:+,.1f} pts)", ""]
    ev = trend_events(d, fut.between_time("09:15", "15:30") if fut is not None else None)
    out.append("TREND (weak-trend score held %d min): " % TREND_HOLD_MIN
               + (", ".join(f"{x} {t:%H:%M}" for t, x in ev) if ev else "none"))
    out.append("")
    out += flow_eod_lines(d, fut.between_time("09:15", "15:30") if fut is not None else None)
    for kind, conf in KINDS:
        row = []
        for sig, name in ((CALL, "CALL"), (PUT, "PUT")):
            times = [f"{t:%H:%M}" for t, k, s, _ in found if k == kind and s == sig]
            row.append(f"{name} {len(times)}" + (" at " + ", ".join(times) if times else ""))
        out.append(f"{kind}{'' if kind in LIVE_KINDS else ' (not sent)'}: " + " | ".join(row))
    if skipped:
        out.append("Skipped (against price vs the day's open): "
                   + ", ".join(f"{'CALL' if sg == CALL else 'PUT'} {k.title()} {t:%H:%M}" for t, k, sg, _ in skipped[:8]))
    if late:
        out.append(f"Blocked by time rule (no BREAKOUT after {BREAKOUT_LAST[0]}:{BREAKOUT_LAST[1]:02d}, "
                   f"nothing after {NO_NEW_AFTER[0]}:{NO_NEW_AFTER[1]:02d}): "
                   + ", ".join(f"{'CALL' if sg == CALL else 'PUT'} {k.title()} {t:%H:%M}" for t, k, sg, _ in late[:8])
                   + (f" +{len(late) - 8} more" if len(late) > 8 else ""))
    if found:
        out += ["", "How each did, points in the signal's direction (30m / 60m: best/worst/net):"]
        total, nets = 0.0, 0
        against_sum = against_n = with_sum = with_n = 0
        merged, order = {}, []
        for t, kind, sig, i in found:
            if kind in ("OPENING BREAK", "SWING"):
                continue
            key = (t, sig, i)                       # same candle + same direction = one trade
            if key not in merged:
                merged[key] = []
                order.append(key)
            merged[key].append(kind)
        for t, kind, sig, i in found:
            if kind == "OPENING BREAK":
                out.append(opening_break_outcome(d, t, sig, i))   # held to the exit time, so not added to the 60m sum
        sw_lines, sw_total, sw_n = swing_outcomes(d)
        if sw_lines:
            out += sw_lines
            out.append(f"Swing trades (held to the next swing signal or {SWING_EXIT[0]}:{SWING_EXIT[1]:02d}): "
                       f"{sw_total:+.0f} pts over {sw_n} trades. Separate from the 60m sum below.")
        for key in order[:30]:
            t, sig, i = key
            kinds = merged[key]
            line, net60 = outcome_line(d, t, " + ".join(kinds), sig, i)
            live = any(k in LIVE_KINDS for k in kinds)
            tagged = ALIGN_MODE != "off" and any(
                k in ALIGN_KINDS and not is_aligned(d.iloc[: i + 1], k, sig) for k in kinds)
            out.append(line + ("  [against open]" if tagged else "") + ("" if live else "  [not sent]"))
            if net60 is not None and live:
                total, nets = total + net60, nets + 1
                if any(k in ALIGN_KINDS for k in kinds):
                    if tagged:
                        against_sum, against_n = against_sum + net60, against_n + 1
                    else:
                        with_sum, with_n = with_sum + net60, with_n + 1
        out.append(f"Sum of 60m nets (alerts that were sent): {total:+.0f} pts over {nets} trades "
                   "(same-minute, same-direction signals counted once)")
        if ALIGN_MODE != "off" and (against_n or with_n):
            out.append(f"In line with the open: {with_n} signals {with_sum:+.0f} | against the open: {against_n} signals {against_sum:+.0f}")
    out += ["", "Not financial advice."]
    return "\n".join(out)


def market_open() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)


def sync_futures(df, fut, fut_key, retries=3, wait=5):
    """The futures candle for the newest minute can arrive a few seconds after the index candle.
    If it is missing, volume and VWAP are unknown and the detectors would let weak signals through.
    So: retry briefly, and if it is still missing, judge only up to the last candle that BOTH feeds have."""
    if fut is None or not len(fut) or not len(df):
        return df, fut
    for _ in range(retries):
        if fut.index[-1] >= df.index[-1]:
            break
        time.sleep(wait)
        try:
            fut = fetch_candles(fut_key)
        except requests.RequestException:
            break
    if len(fut) and fut.index[-1] < df.index[-1]:
        lag = (df.index[-1] - fut.index[-1]).total_seconds() / 60
        if lag <= 3:
            df = df[df.index <= fut.index[-1]]
    return df, fut


def main():
    print(f"Scanning {INDEX_KEY} every {REFRESH_SECONDS}s (multi-signal mode, v8.4).\n")
    fut_key, fut_name = None, None
    try:
        fut_key, fut_name = get_future_key()
    except Exception as e:
        print(f"Futures lookup failed ({e}). Running without volume.")
    print(f"Futures contract: {fut_name}")
    summary_only = [k for k, _ in KINDS if k not in LIVE_KINDS]
    relay_run = os.environ.get("RELAY", "").lower() == "yes"
    if relay_run:
        send_telegram("Scanner continuing in a fresh GitHub run (handover done). Alerts carry on as normal.")
    else:
        send_telegram(f"Nifty scanner v8.9 started{' (accuracy mode: fewer, stricter alerts)' if ACCURACY_MODE else ''}. Live alerts: {', '.join(LIVE_KINDS)}"
                      f"{' + WAIT notes' if SEND_WAIT_NOTES else ''}. "
                      + (f"Summary only: {', '.join(summary_only)}. " if summary_only else "")
                      + f"No BREAKOUT after {BREAKOUT_LAST[0]}:{BREAKOUT_LAST[1]:02d}, nothing new after "
                        f"{NO_NEW_AFTER[0]}:{NO_NEW_AFTER[1]:02d}. "
                      + (f"Against-the-open signals are {'tagged' if ALIGN_MODE == 'tag' else 'blocked'}. " if ALIGN_MODE != "off" else "")
                      + f"SWING alert: a {SWING_PTS}-pt reversal off a day top or bottom (late by design). "
                      + f"Volume source: {fut_name or 'none (volume unverified)'}.")

    last_candle, obs_count, st = None, 0, State()
    st.opening_read_done = relay_run          # a hand-over run must not repeat the 09:30 read
    try:
        st.prev_close = fetch_prev_close()
    except Exception as e:
        print(f"Previous close not available ({e}); the opening read will skip the gap.")
    st.trend_reported = os.environ.get("RELAY_TREND") or None   # so the handover does not repeat the TREND message
    started = time.time()
    while True:
        try:
            now = datetime.now(IST)
            if now.weekday() < 5 and (now.hour, now.minute) > (15, 30):
                try:
                    df = fetch_candles(INDEX_KEY)
                    fut = fetch_candles(fut_key) if fut_key else None
                    summary = eod_summary(df, fut)
                    print(summary)
                    send_telegram(summary)
                except Exception as e:
                    print(f"End-of-day summary failed: {e}")
                    send_telegram("Market closed. Scanner stopped for the day.")
                print("Market closed. Stopping.")
                break
            if MAX_RUN_MIN and (time.time() - started) / 60 >= MAX_RUN_MIN and market_open():
                with open("relay_needed", "w") as fh:        # the workflow starts the next run when it sees this file
                    fh.write(st.trend_reported or "")
                send_telegram("Handing over to a fresh GitHub run now (GitHub stops a job after 6 hours). "
                              "Alerts continue in about a minute.")
                print("Relay hand-over requested. Stopping this run.")
                break
            if not market_open():
                print("Waiting for market open (9:15 IST, Mon-Fri)...")
            else:
                df = fetch_candles(INDEX_KEY)
                if not len(df) and (now.hour, now.minute) >= (9, 35):
                    # 20 minutes after the open and still no candles: a market holiday or no data
                    send_telegram("No Nifty candles today, so the market looks closed (holiday?). "
                                  "Scanner stopping for the day.")
                    print("No candles after 09:35 on a weekday. Stopping (holiday?).")
                    break
                fut = None
                if fut_key:
                    try:
                        fut = fetch_candles(fut_key)
                    except requests.RequestException as e:
                        print(f"  Futures fetch failed: {e}")
                df, fut = sync_futures(df, fut, fut_key)
                if len(df) and df.index[-1] != last_candle:  # evaluate each candle once
                    last_candle = df.index[-1]
                    obs = observation(df, fut)
                    print(obs.replace("\n", " | "))
                    obs_count += 1
                    if OBSERVE_TELEGRAM_EVERY and obs_count % OBSERVE_TELEGRAM_EVERY == 0:
                        send_telegram(obs, silent=True)
                    if (OPTION_DATA or OPTION_CSV) and len(df):
                        option_snapshot(float(df["Close"].iloc[-1]))   # keeps the option CSV filled every 5 minutes
                    if len(df) >= IND_MIN_CANDLES:
                        for m in process_candle(df, fut, now, st):
                            print(m.replace("\n", " | "))
                            send_telegram(m)
                    if len(df) >= FULL_CANDLES:
                        _, _, why, _ = analyze(df, fut)
                        for w in why:
                            print(f"    {w}")
        except requests.RequestException as e:
            print(f"Network/API error: {e}")
        time.sleep(max(5, 68 - datetime.now(IST).second))  # wake at ~:08 of the next minute


if __name__ == "__main__":
    main()
