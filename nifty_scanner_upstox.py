"""
Nifty 50 multi-signal scanner v7 (Upstox) -> Telegram alerts. Places NO orders.
Educational tool only. Not financial advice.

v7 changes, all based on a replay of 12 trading days (8-26 Oct 2026). Small sample, so re-test on new days:
  1. TIME RULES   no new alert after 13:30 (25 signals after 13:30 lost 317 pts net at 60 min),
                  and no BREAKOUT after 12:00 (before 12:00: 10 of 13 won; after 12:00 it was flat or negative).
  2. INDICATION   no longer sent live (10 signals, 3 won, -637 pts at 60 min). Still counted in the end-of-day summary.
  3. OPENING BREAK now sent live, with a stop at the middle of the first-15-minute range and an exit idea at 15:15
                  (10 trades, 7 won, +1,179 pts held to 15:15). A "stop hit" alert follows if price touches the stop.
  4. The day's high or low was set by 09:48 on all 12 days, so the opening-range extremes are the levels that matter.
  5. OPTION CHECK (v7.1) every live alert shows the at-the-money option's bid/ask spread and an estimate of how many
                  index points the move must be just to cover spread and time decay, compared with the signal's typical
                  move. Verdict OK / CAUTION / SKIP. Set OPT_MODE = "block" to suppress alerts that look too costly.

Alert types (each labelled with a confidence level):
  OPENING BREAK  MEDIUM   first close beyond the 09:15-09:29 range before 10:00 (earliest entry, has a stop idea)
  BREAKOUT       HIGH     strong, high-volume break of the day's range that holds 2 candles (before 12:00 only)
  FAKE BREAKOUT  LOWER    high-volume break that fails and closes back inside the range
  PULLBACK       LOWER    trend continuation: bounce against the trend fails
  TREND REVERSE  LOWER    fresh EMA cross + VWAP flip + break of recent structure
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

import pandas as pd
import requests

INDEX_KEY = "NSE_INDEX|Nifty 50"
INSTR_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
REFRESH_SECONDS = 60

# ---- v7 time rules (edit to tune) ----
NO_NEW_AFTER = (13, 30)      # no new alert of any kind from this time
BREAKOUT_LAST = (12, 0)      # BREAKOUT is only sent before this time

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
PULL_NO_ENTRY_AFTER = (14, 45)
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
LIVE_KINDS = ("OPENING BREAK", "BREAKOUT", "FAKE BREAKOUT", "PULLBACK", "TREND REVERSE")
SEND_WAIT_NOTES = False

# ---- Alignment filter: signals that go against price vs the day's open ----
ALIGN_MODE = "tag"            # "tag" = send but mark "against the open"; "block" = skip them; "off" = ignore
ALIGN_KINDS = ("INDICATION", "BREAKOUT", "FAKE BREAKOUT", "PULLBACK")

# ---- Opening read (09:30) and OPENING BREAK ----
SEND_OPENING_READ = True
OPENING_BREAK_UNTIL = (10, 0)   # first close beyond the first-15-minute range, before this time
OB_EXIT = (15, 15)              # exit idea for an opening break

# ---- v7.1 option check: a wide spread and time decay can eat a small index move ----
OPT_MODE = "tag"             # "tag" = add the option line to the alert; "block" = skip alerts that look too costly; "off" = no check
OPT_STRIKE_OFFSET = 0        # 0 = at-the-money; 1 = one strike (OPT_STEP pts) further out of the money
OPT_STEP = 50                # Nifty strike step
OPT_HOLD_MIN = 60            # minutes the trade is assumed to be held (OPENING BREAK: until OB_EXIT)
OPT_DAY_MINUTES = 375        # trading minutes per day; daily theta is spread over these to get decay for the hold time
OPT_OK_SHARE = 0.35          # costs up to 35% of the signal's typical move = OK
OPT_SKIP_SHARE = 0.60        # costs above 60% of the typical move = SKIP (in between = CAUTION)
OPT_MAX_SPREAD_PCT = 3.0     # bid/ask spread above 3% of the premium = SKIP
OPT_CACHE_SEC = 20           # reuse one option-chain download for this long
# Typical move in index points per signal, from the 12-day replay (60-minute result; OPENING BREAK held to 15:15).
TYPICAL_MOVE_PTS = {"OPENING BREAK": 118, "BREAKOUT": 57, "PULLBACK": 22, "TREND REVERSE": 15, "FAKE BREAKOUT": 9}

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
    """Returns (futures key, futures name, nearest Nifty option expiry as YYYY-MM-DD).
    Futures: nearest expiry, skipping the last 2 days."""
    r = requests.get(INSTR_URL, timeout=60)
    r.raise_for_status()
    data = json.loads(gzip.decompress(r.content))
    cutoff = (datetime.now(IST) + timedelta(days=2)).timestamp() * 1000
    futs = [i for i in data
            if i.get("segment") == "NSE_FO" and i.get("instrument_type") == "FUT"
            and i.get("underlying_symbol") == "NIFTY" and i.get("expiry", 0) > cutoff]
    today_ms = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    exps = sorted({i["expiry"] for i in data
                   if i.get("segment") == "NSE_FO" and i.get("instrument_type") in ("CE", "PE")
                   and i.get("underlying_symbol") == "NIFTY" and i.get("expiry", 0) >= today_ms})
    opt_expiry = datetime.fromtimestamp(exps[0] / 1000, IST).strftime("%Y-%m-%d") if exps else None
    if not futs:
        return None, None, opt_expiry
    f = min(futs, key=lambda i: i["expiry"])
    return f["instrument_key"], f.get("trading_symbol", "NIFTY FUT"), opt_expiry


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
    return line


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
    if n < 30 or (df.index[-1].hour, df.index[-1].minute) >= PULL_NO_ENTRY_AFTER:
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
         ("BREAKOUT", "HIGH confidence (10 of 13 won before 12:00)"),
         ("FAKE BREAKOUT", "LOWER confidence (only 5 seen, 2 won)"),
         ("PULLBACK", "LOWER confidence (59 seen, 58% won)"),
         ("TREND REVERSE", "LOWER confidence (20 seen, 60% won, small gains)"),
         ("OPENING BREAK", "MEDIUM confidence (7 of 10 won with the stop)"))


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
    return {"signal": CALL if up else PUT, "key": f"{t:%Y-%m-%d}", "entry": c, "stop": mid, "d": 1 if up else -1,
            "why": [f"First close {'above' if up else 'below'} the opening range {lo:,.1f} to {hi:,.1f}",
                    f"Stop idea: the range middle {mid:,.1f} ({abs(c - mid):.0f} pts risk). "
                    f"Exit idea: {OB_EXIT[0]}:{OB_EXIT[1]:02d}"]}


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


def fetch_option_chain(expiry: str):
    """Upstox put/call option chain for one expiry: a list of strikes with call_options / put_options."""
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    r = requests.get("https://api.upstox.com/v2/option/chain",
                     params={"instrument_key": INDEX_KEY, "expiry_date": expiry},
                     headers={"Accept": "application/json", "Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return r.json().get("data") or []


def option_quote(chain, spot, signal):
    """The at-the-money option (plus OPT_STRIKE_OFFSET) for this signal. Returns a dict, or None if it has no live quote."""
    rows = [r for r in chain if r.get("strike_price")]
    if not rows:
        return None
    call = signal == CALL
    atm = round(spot / OPT_STEP) * OPT_STEP
    want = atm + (OPT_STRIKE_OFFSET if call else -OPT_STRIKE_OFFSET) * OPT_STEP
    row = min(rows, key=lambda r: abs(r["strike_price"] - want))
    opt = row.get("call_options" if call else "put_options") or {}
    md, gk = opt.get("market_data") or {}, opt.get("option_greeks") or {}
    bid, ask = md.get("bid_price") or 0, md.get("ask_price") or 0
    if not (bid > 0 and ask >= bid):
        return None
    return {"strike": row["strike_price"], "kind": "CE" if call else "PE", "bid": float(bid), "ask": float(ask),
            "delta": abs(gk.get("delta") or 0), "theta": gk.get("theta") or 0}


def option_verdict(q, kinds, hm):
    """Cost of trading the option for a signal. Returns (text, verdict). Pure function, no network."""
    mid, spread = (q["bid"] + q["ask"]) / 2, q["ask"] - q["bid"]
    spread_pct = spread / mid * 100
    delta = q["delta"] or 0.5                       # if the feed gives no delta, assume about 0.5 at-the-money
    hold = OPT_HOLD_MIN
    if "OPENING BREAK" in kinds:                    # held until the exit time
        hold = max(OPT_HOLD_MIN, OB_EXIT[0] * 60 + OB_EXIT[1] - (hm[0] * 60 + hm[1]))
    decay = abs(q["theta"]) * hold / OPT_DAY_MINUTES   # assumes the feed's theta is per day
    be_pts = (spread + decay) / delta               # index points needed just to cover spread + decay
    typical = max(TYPICAL_MOVE_PTS.get(k, 20) for k in kinds)
    share = be_pts / typical
    verdict = ("SKIP" if spread_pct > OPT_MAX_SPREAD_PCT or share > OPT_SKIP_SHARE
               else "OK" if share <= OPT_OK_SHARE else "CAUTION")
    theta_txt = f"theta {q['theta']:+.1f}/day" if q["theta"] else "theta n/a (decay not counted)"
    text = (f"OPTION {q['strike']:,.0f} {q['kind']}: bid {q['bid']:,.2f} / ask {q['ask']:,.2f}, "
            f"spread {spread:.2f} ({spread_pct:.1f}%)\n"
            f"Delta {delta:.2f}, {theta_txt}. To break even over ~{hold} min the index must move about {be_pts:.0f} pts "
            f"(spread {spread / delta:.0f} + decay {decay / delta:.0f}).\n"
            f"Typical move for this signal ~{typical} pts, so costs take about {share:.0%}. Option check: {verdict}")
    return text, verdict


def option_check(st, spot, signal, kinds, now, hm):
    """Fetch the option quote and judge it. Returns (text, verdict). Never raises; if data is missing the verdict is None."""
    if OPT_MODE == "off" or not st.opt_expiry:
        return "", None
    unavailable = "OPTION: quote unavailable, check the premium and spread yourself before trading."
    try:
        c = st.chain_cache
        if not c or (now - c[0]).total_seconds() > OPT_CACHE_SEC:
            c = (now, fetch_option_chain(st.opt_expiry))
            st.chain_cache = c
        q = option_quote(c[1], spot, signal)
    except Exception as e:
        print(f"  Option chain failed: {e}")
        return unavailable, None
    if q is None:
        return unavailable, None
    text, verdict = option_verdict(q, kinds, hm)
    if st.opt_expiry == now.strftime("%Y-%m-%d"):
        text += "\nEXPIRY DAY: premiums swing sharply, use a smaller size."
    return text, verdict


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
        self.opt_expiry = None  # nearest option expiry, YYYY-MM-DD
        self.chain_cache = None

    def allow(self, kind, key, now) -> bool:
        if key == self.last_key.get(kind):
            return False
        lt = self.last_time.get(kind)
        cool = IND_COOLDOWN_MIN if kind == "INDICATION" else COOLDOWN_MIN
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
    # --- TREND status: context only, sent when the held direction changes
    if SEND_TREND:
        td = trend_direction_held(df, fut)
        if td and td != st.trend_reported:
            st.trend_reported = td
            up = td == CALL
            msgs.append(f"TREND {'UP' if up else 'DOWN'} (held {TREND_HOLD_MIN} min)\n"
                        f"Nifty {price:,.2f} at {ts} IST\n"
                        f"The weak-trend score has pointed {'up' if up else 'down'} for {TREND_HOLD_MIN} minutes in a row.\n"
                        f"Bias: favour {'CALL' if up else 'PUT'} setups and be careful with the opposite side.\n"
                        f"Context only, not an entry. Choppy days flip it often.\n\nNot financial advice.")
    fired = False
    send = [s for s in sigs
            if time_ok(df, s["kind"]) and aligned_ok(df, s["kind"], s["signal"])
            and st.allow(s["kind"], s["key"], now) and s["kind"] in LIVE_KINDS]
    groups = {}
    for s in send:
        groups.setdefault(s["signal"], []).append(s)      # same candle + same direction = one alert
    for signal, grp in groups.items():
        fired = True
        upd = "UPDATE: this confirms the earlier WAIT note.\n" if st.watch else ""
        kinds_txt = " + ".join(s["kind"] for s in grp)
        opt_text, verdict = option_check(st, price, signal, [s["kind"] for s in grp], now, hm)
        if OPT_MODE == "block" and verdict == "SKIP":
            msgs.append(f"OPTION CHECK SKIPPED a {signal} alert ({kinds_txt}) at {ts}\n{opt_text}\n\nNot financial advice.")
            continue
        optline = (opt_text + "\n") if opt_text else ""
        for s in grp:
            if s["kind"] == "OPENING BREAK":
                st.ob = {"d": s["d"], "entry": s["entry"], "stop": s["stop"], "time": ts}
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
        msgs.append(f"{head}{upd}{note}Nifty {price:,.2f} at {ts} IST\n{body}\n{optline}"
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
        sigs, _ = scan_candle(d, f)
        for s in sigs:
            if not time_ok(d, s["kind"]):
                if late is not None and st_late.allow(s["kind"], s["key"], d.index[-1]):
                    late.append((d.index[-1], s["kind"], s["signal"], i))
                continue
            if not aligned_ok(d, s["kind"], s["signal"]):
                if skipped is not None and st_skip.allow(s["kind"], s["key"], d.index[-1]):
                    skipped.append((d.index[-1], s["kind"], s["signal"], i))
                continue
            if st.allow(s["kind"], s["key"], d.index[-1]):
                found.append((d.index[-1], s["kind"], s["signal"], i))
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
            if kind == "OPENING BREAK":
                continue
            key = (t, sig, i)                       # same candle + same direction = one trade
            if key not in merged:
                merged[key] = []
                order.append(key)
            merged[key].append(kind)
        for t, kind, sig, i in found:
            if kind == "OPENING BREAK":
                out.append(opening_break_outcome(d, t, sig, i))   # held to the exit time, so not added to the 60m sum
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
    print(f"Scanning {INDEX_KEY} every {REFRESH_SECONDS}s (multi-signal mode, v7).\n")
    fut_key, fut_name, opt_expiry = None, None, None
    try:
        fut_key, fut_name, opt_expiry = get_future_key()
    except Exception as e:
        print(f"Futures lookup failed ({e}). Running without volume.")
    print(f"Futures contract: {fut_name}. Option expiry: {opt_expiry}")
    summary_only = [k for k, _ in KINDS if k not in LIVE_KINDS]
    relay_run = os.environ.get("RELAY", "").lower() == "yes"
    if relay_run:
        send_telegram("Scanner continuing in a fresh GitHub run (handover done). Alerts carry on as normal.")
    else:
        send_telegram(f"Nifty scanner v7 started. Live alerts: {', '.join(LIVE_KINDS)}"
                      f"{' + WAIT notes' if SEND_WAIT_NOTES else ''}. "
                      + (f"Summary only: {', '.join(summary_only)}. " if summary_only else "")
                      + f"No BREAKOUT after {BREAKOUT_LAST[0]}:{BREAKOUT_LAST[1]:02d}, nothing new after "
                        f"{NO_NEW_AFTER[0]}:{NO_NEW_AFTER[1]:02d}. "
                      + (f"Against-the-open signals are {'tagged' if ALIGN_MODE == 'tag' else 'blocked'}. " if ALIGN_MODE != "off" else "")
                      + f"Option check: {OPT_MODE}" + (f" (expiry {opt_expiry}). " if opt_expiry else " (no expiry found, off). ")
                      + f"Volume source: {fut_name or 'none (volume unverified)'}.")

    last_candle, obs_count, st = None, 0, State()
    st.opening_read_done = relay_run          # a hand-over run must not repeat the 09:30 read
    st.opt_expiry = opt_expiry
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
