"""
Nifty 50 multi-signal scanner (Upstox) -> Telegram alerts. Places NO orders.
Educational tool only. Not financial advice.

Alert types (each labelled with a confidence level):
  INDICATION     LOW      early read on 8-24 candles (about 09:22), confirmed or not at 25 candles
  BREAKOUT       HIGH     strong, high-volume break of the day's range that holds 2 candles
  FAKE BREAKOUT  MEDIUM   high-volume break that fails and closes back inside the range
  PULLBACK       LOWER    trend continuation: bounce against the trend fails
  TREND REVERSE  LOWER    fresh EMA cross + VWAP flip + break of recent structure
  TREND          info     UP / DOWN status when the weak-trend score holds 10 min (context, not an entry)
  WAIT           info     "observing" note when a setup is developing, then an UPDATE message

BREAKOUT needs ALL of: close beyond opening range and 30-min high/low, strong candle,
futures volume >= 1.5x, 2 candles hold, not chasing, EMA/VWAP/RSI agree, market moving.

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

# ---- Strong-breakout settings (edit to tune) ----
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

# ---- Early INDICATION (opening candles, before the full 25-candle checks) ----
IND_MIN_CANDLES = 8          # earliest candle count for an indication (about 09:22)
FULL_CANDLES = 25            # at this many candles the full checks run and the indication is confirmed or not
IND_EXT_WINDOW = 8           # the session low/high must be within the last 8 candles
IND_FLIP_WINDOW = 6          # futures must have flipped across VWAP within the last 6 candles
IND_INVALID_WINDOW = 6       # invalidation level = extreme of the last 6 candles
IND_COOLDOWN_MIN = 3         # minutes between indications

# ---- Extra signal types ----
FAKE_WINDOW = 8              # a failed breakout is spotted up to 8 candles after the break
FAKE_VOL_RATIO = 1.5         # the failed break must have had at least this volume
FAKE_MARGIN_PCT = 0.05       # price must close back inside the level by this % (~11 pts)
PULL_BOUNCE_WINDOW = 6       # pullback: price must have bounced against the trend in the last 6 candles
PULL_NO_ENTRY_AFTER = (14, 45)
PULL_MIN_RANGE_PCT = 0.20     # pullback needs the last 30 minutes to have moved at least this % (0 = off)
REV_CROSS_WINDOW = 10        # reversal: EMA9/21 cross must be this fresh
REV_VWAP_WINDOW = 15         # reversal: price was on the other side of VWAP within this window
REV_STRUCT_WINDOW = 15       # reversal: must break the last 15 candles' high/low
REV_VOL_RATIO = 1.2          # reversal: volume at least this vs average
WATCH_PCT = 0.08             # "near the level" = within this % of a break level (~18 pts)
WATCH_VOL_RATIO = 3.0        # a candle with this much futures volume starts an observation
WATCH_TIMEOUT_MIN = 10       # send an UPDATE if nothing confirms within this time
WATCH_COOLDOWN_MIN = 15      # minutes between WAIT / observing notes

# ---- TREND status message (context only, not an entry signal) ----
SEND_TREND = True            # send "TREND UP / DOWN" when the weak-trend score holds for TREND_HOLD_MIN minutes
TREND_HOLD_MIN = 10          # minutes in a row the weak-trend score must point the same way

# ---- GitHub relay: a GitHub job is stopped after 6 hours, so the scanner hands over to a fresh run ----
MAX_RUN_MIN = float(os.environ.get("SCAN_MAX_MINUTES", "0") or 0)   # 0 = never hand over (server use)

# ---- Observation mode: what is sent live vs only counted in the end-of-day summary ----
LIVE_KINDS = ("INDICATION", "BREAKOUT", "FAKE BREAKOUT", "PULLBACK", "TREND REVERSE")   # every type is sent live
SEND_WAIT_NOTES = False      # True = send "WAIT - observing" notes and their UPDATE messages

# ---- Alignment filter: only send signals that agree with price vs the day's open ----
ALIGN_FILTER = True          # 8-day test: agreeing signals +1,205 pts, disagreeing -330 pts
ALIGN_KINDS = ("INDICATION", "BREAKOUT", "FAKE BREAKOUT", "PULLBACK")   # TREND REVERSE is exempt (it is against the old direction by design)

# ---- Opening read (09:30) and OPENING BREAK (observed in the end-of-day summary, not sent live) ----
SEND_OPENING_READ = True
OPENING_BREAK_UNTIL = (10, 0)   # first close beyond the first-15-minute range, before this time

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
    Used only for the end-of-day count, never sent as a live alert."""
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
    """INDICATION: early reversal read on 8-24 candles. Session extreme just made, futures flipped
    across VWAP, and a candle closing beyond the previous two. Low confidence by design."""
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


KINDS = (("INDICATION", "LOW confidence (early, under 25 candles)"),
         ("BREAKOUT", "HIGH confidence"), ("FAKE BREAKOUT", "MEDIUM confidence"),
         ("PULLBACK", "LOWER confidence (only 2 days tested)"),
         ("TREND REVERSE", "LOWER confidence (only 2 days tested)"),
         ("OPENING BREAK", "OBSERVE only (stop at the range middle)"))


def aligned_ok(df, kind, signal) -> bool:
    """Alignment filter: a signal must agree with price vs the day's open at that moment."""
    if not ALIGN_FILTER or kind not in ALIGN_KINDS:
        return True
    d = 1 if signal == CALL else -1
    return d * (df["Close"].iloc[-1] - df["Open"].iloc[0]) > 0


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
    return {"signal": CALL if up else PUT, "key": f"{t:%Y-%m-%d}", "entry": c, "stop": float((hi + lo) / 2),
            "why": [f"First close {'above' if up else 'below'} the opening range {lo:,.1f} to {hi:,.1f}",
                    f"Stop idea: the range middle {(hi + lo) / 2:,.1f}. Exit idea: 15:15"]}


def opening_break_outcome(d, t, sig, i):
    """Result of an opening break held to 15:15, with a stop at the range middle."""
    sign = 1 if sig == CALL else -1
    entry = d["Close"].iloc[i]
    hi, lo = d["High"].iloc[:OPENING_RANGE_MIN].max(), d["Low"].iloc[:OPENING_RANGE_MIN].min()
    stop, res, how = (hi + lo) / 2, None, "15:15"
    for j in range(i + 1, len(d)):
        ts = d.index[j]
        if (ts.hour, ts.minute) >= (15, 15):
            res = sign * (d["Close"].iloc[j] - entry)
            break
        if (sign > 0 and d["Low"].iloc[j] <= stop) or (sign < 0 and d["High"].iloc[j] >= stop):
            res, how = sign * (stop - entry), f"stopped {ts:%H:%M}"
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
              f"Break levels: a close above {hi:,.1f} = UP break, below {lo:,.1f} = DOWN break.",
              "Context only. The first-15-minute direction matched the day on 7 of 8 days so far "
              "(the miss was a V-reversal).",
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
    for kind, fn in (("FAKE BREAKOUT", detect_fake), ("PULLBACK", detect_pullback),
                     ("TREND REVERSE", detect_reverse)):
        r = fn(df, ind)
        if r:
            out.append({"kind": kind, **r})
    return out, ind


class State:
    """Per-day memory: cooldowns, de-duplication and the current WAIT / observing note."""

    def __init__(self):
        self.last_time, self.last_key = {}, {}
        self.watch, self.watch_since, self.last_watch = None, None, None
        self.indication = None
        self.trend_reported = None
        self.prev_close = None
        self.opening_read_done = False

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
    # --- OPENING READ: once, as soon as the first 15 minutes are complete
    if SEND_OPENING_READ and not st.opening_read_done and n >= OPENING_RANGE_MIN:
        st.opening_read_done = True
        msgs.append(opening_read_message(df, st.prev_close))
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
    # --- an early INDICATION is either invalidated, or confirmed / not confirmed at 25 candles
    a = st.indication
    if a:
        side = "CALL" if a["d"] > 0 else "PUT"
        if a["d"] * (price - a["inval"]) < 0:
            msgs.append(f"INDICATION INVALIDATED ({ts})\nNifty {price:,.2f} closed "
                        f"{'below' if a['d'] > 0 else 'above'} {a['inval']:,.1f}. "
                        f"The early {side} idea from {a['time']} no longer holds. Wait.")
            st.indication = None
        elif n >= FULL_CANDLES:
            ema_ok = a["d"] * (ind["ema9"].iloc[-1] - ind["ema21"].iloc[-1]) > 0
            gap = ind["vwap_gap"].iloc[-1]
            vwap_ok = pd.isna(gap) or a["d"] * gap > 0
            held = a["d"] * (price - a["entry"]) >= 0
            checks = [("EMA9 vs EMA21", ema_ok), ("Futures vs VWAP", vwap_ok), ("Price beyond indication entry", held)]
            verdict = "CONFIRMED" if all(c for _, c in checks) else "NOT CONFIRMED"
            msgs.append(f"{verdict}: early {side} indication from {a['time']} at {a['entry']:,.1f}\n"
                        f"Nifty {price:,.2f} at {ts} IST after {n} candles\n"
                        + "\n".join(f"{'✓' if c else '✗'} {name}" for name, c in checks)
                        + ("\nNormal alerts take over from here." if verdict == "CONFIRMED"
                           else "\nThe early idea is not supported. No trade on it.")
                        + "\n\nNot financial advice.")
            st.indication = None
    fired = False
    for s in sigs:
        if not aligned_ok(df, s["kind"], s["signal"]):
            continue  # against price vs the day's open: skipped (the summary lists it)
        if st.allow(s["kind"], s["key"], now) and s["kind"] in LIVE_KINDS:
            fired = True
            upd = "UPDATE: this confirms the earlier WAIT note.\n" if st.watch else ""
            extra = ""
            if s["kind"] == "INDICATION":
                side = "below" if s["d"] > 0 else "above"
                replaced = (f"Replaces the earlier {'CALL' if st.indication['d'] > 0 else 'PUT'} indication.\n"
                            if st.indication else "")
                extra = f"{replaced}Invalidated if Nifty closes {side} {s['inval']:,.1f}.\n"
                st.indication = {"d": s["d"], "entry": s["entry"], "inval": s["inval"], "time": ts}
            msgs.append(f"{s['kind']}: {s['signal']}\n{conf[s['kind']]}\n{upd}"
                        f"Nifty {price:,.2f} at {ts} IST\n" + "\n".join(s["why"]) + "\n" + extra
                        + "\nNot financial advice. Use your own stop-loss.")
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


def day_signals(df: pd.DataFrame, fut, skipped=None):
    """Replay the day candle by candle with the same rules as live.
    Returns a list of (time, kind, signal, candle_index). If `skipped` is a list, signals removed by the
    alignment filter are appended to it (same tuple format)."""
    st, st_skip, found = State(), State(), []
    for i in range(IND_MIN_CANDLES - 1, len(df)):
        d = df.iloc[: i + 1]
        f = fut[fut.index <= d.index[-1]] if fut is not None else None
        sigs, _ = scan_candle(d, f)
        for s in sigs:
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
        best, worst = max(0.0, best), max(0.0, worst)  # never show "--0" when price never went against us
        parts.append(f"{mins}m +{best:.0f}/-{worst:.0f}/{net:+.0f}")
        net60 = net
    return (f"{t:%H:%M} {'CALL' if sign > 0 else 'PUT'} {kind} @{entry:,.1f} | " + " | ".join(parts)), net60


def eod_summary(df: pd.DataFrame, fut) -> str:
    """End-of-day summary: signals by type with times, and how each one did."""
    d = df.between_time("09:15", "15:30")
    skipped = []
    found = day_signals(d, fut.between_time("09:15", "15:30") if fut is not None else None, skipped)
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
        out.append(f"{kind} ({conf.split()[0]}): " + " | ".join(row))
    if skipped:
        out.append("Skipped (against price vs the day's open): "
                   + ", ".join(f"{'CALL' if sg == CALL else 'PUT'} {k.title()} {t:%H:%M}" for t, k, sg, _ in skipped[:8]))
    if found:
        out += ["", "How each did, points in the signal's direction (30m / 60m: best/worst/net):"]
        total, nets = 0.0, 0
        for t, kind, sig, i in found[:30]:
            if kind == "OPENING BREAK":
                out.append(opening_break_outcome(d, t, sig, i))   # held to 15:15, so not added to the 60m sum
                continue
            line, net60 = outcome_line(d, t, kind, sig, i)
            out.append(line)
            if net60 is not None:
                total, nets = total + net60, nets + 1
        out.append(f"Sum of 60m nets: {total:+.0f} pts over {nets} signals")
    out += ["", "Not financial advice."]
    return "\n".join(out)


def market_open() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)


def sync_futures(df, fut, fut_key, retries=3, wait=5):
    """The futures candle for the newest minute can arrive a few seconds after the index candle.
    If it is missing, volume and VWAP are unknown and the detectors would let weak signals through
    (this caused false alerts on 13 Oct). So: retry briefly, and if it is still missing, judge only
    up to the last candle that BOTH feeds have."""
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
    print(f"Scanning {INDEX_KEY} every {REFRESH_SECONDS}s (multi-signal mode).\n")
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
        send_telegram(f"Nifty scanner v6 started. Live alerts: {', '.join(LIVE_KINDS)}"
                      f"{' + WAIT notes' if SEND_WAIT_NOTES else ''}. "
                      + (f"Summary only: {', '.join(summary_only)}. " if summary_only else "")
                      + ("Alignment filter ON. " if ALIGN_FILTER else "")
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
