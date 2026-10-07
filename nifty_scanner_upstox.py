"""
Nifty 50 STRONG-BREAKOUT scanner (Upstox) -> Telegram alerts. Places NO orders.
Educational tool only. Not financial advice.

An alert is sent only when ALL of these pass:
  1. Breakout: a candle closes beyond the opening range AND the last 30-min high/low
  2. Strong candle: big body (>=60% of range) and bigger than the recent average
  3. Volume: Nifty futures volume on that candle >= 1.5x its recent average
  4. Hold: the next 2 candles stay beyond the level (no fake-out)
  5. Not chasing: price is not already too far past the level
  6. Trend: EMA9/EMA21 agree, futures on the right side of VWAP, RSI showing momentum (not already exhausted)
  7. Market is actually moving (30-min range above a minimum)

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
OBSERVE_TELEGRAM_EVERY = 0   # silent price+volume update to Telegram every N minutes (0 = off)

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
        raise SystemExit("Set the UPSTOX_ACCESS_TOKEN environment variable first.")
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{quote(key, safe='')}/minutes/1"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code == 401:
        raise SystemExit("Token expired or invalid. Generate a new Upstox access token.")
    r.raise_for_status()
    candles = r.json()["data"]["candles"]  # [ts, o, h, l, c, volume, oi], newest first
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


def side_checks(df: pd.DataFrame, fut, d: int):
    """Checklist for one side. d=+1 breakout up (CALL), d=-1 breakdown (PUT).
    Each check is (True/False/None, text); None = informational only."""
    n = len(df)
    t = n - 1 - HOLD_MIN                      # index of the breakout candle
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
    checks.append((held, f"Held {word} the level for {HOLD_MIN} candles"))
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
    """Returns (signal, score, reasons, breakout_candle_time)."""
    if len(df) < 25:
        return WAIT, 0, ["Not enough candles yet (need ~25)."], None
    sides = {1: side_checks(df, fut, 1), -1: side_checks(df, fut, -1)}
    passes = lambda cs: sum(1 for ok, _ in cs if ok is True)
    d = 1 if passes(sides[1][1]) >= passes(sides[-1][1]) else -1
    t_time, checks = sides[d]
    ok = all(c is not False for c, _ in checks)
    why = [("✓" if c is True else "✗" if c is False else "•") + " " + txt for c, txt in checks]
    signal = (CALL if d > 0 else PUT) if ok else WAIT
    return signal, d * passes(checks), why, t_time


def observation(df: pd.DataFrame, fut) -> str:
    """One-line price + volume summary of the last closed 1-minute candle."""
    k = df.iloc[-1]
    move = k.Close - k.Open
    line = (f"{df.index[-1]:%H:%M} Nifty O {k.Open:,.1f} H {k.High:,.1f} L {k.Low:,.1f} "
            f"C {k.Close:,.1f} ({move:+.1f}, {'UP' if move > 0 else 'DOWN' if move < 0 else 'FLAT'})")
    if fut is not None and len(fut) >= 22 and fut.index[-1] == df.index[-1]:
        v = float(fut["Volume"].iloc[-1])
        base = fut["Volume"].iloc[-21:-1].mean()
        ratio = f"{v / base:.1f}x avg" if base > 0 else "n/a"
        line += f"\nFutures volume {v:,.0f} ({ratio})"
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


def day_signals(df: pd.DataFrame, fut):
    """Replay the day candle by candle. Returns (strong, early) lists of (time, signal)."""
    strong, early, seen, prev_e = [], [], set(), WAIT
    for i in range(25, len(df)):
        d = df.iloc[: i + 1]
        f = fut[fut.index <= d.index[-1]] if fut is not None else None
        sig, _, _, trig = analyze(d, f)
        if sig != WAIT and trig not in seen:
            seen.add(trig)
            strong.append((trig, sig))
        e = early_signal(d, f)
        if e != WAIT and e != prev_e:
            early.append((d.index[-1], e))
        prev_e = e
    return strong, early


def eod_summary(df: pd.DataFrame, fut) -> str:
    """End-of-day summary: how many CALL / PUT signals, and when."""
    d = df.between_time("09:15", "15:30")
    strong, early = day_signals(d, fut.between_time("09:15", "15:30") if fut is not None else None)

    def lines(title, items):
        out = [title]
        for sig, name in ((CALL, "BUY CALL"), (PUT, "BUY PUT")):
            times = [f"{t:%H:%M}" for t, x in items if x == sig]
            out.append(f"  {name}: {len(times)}" + (" at " + ", ".join(times) if times else ""))
        return out

    first, last = d.iloc[0], d.iloc[-1]
    chg = last.Close - first.Open
    out = [f"SIGNALS TODAY - Nifty {d.index[-1]:%d %b %Y}",
           f"Open {first.Open:,.1f} | High {d['High'].max():,.1f} | Low {d['Low'].min():,.1f} | "
           f"Close {last.Close:,.1f} ({chg:+,.1f} pts)", ""]
    out += lines("STRONG breakouts (live alerts; time = breakout candle):", strong)
    out += [""] + lines("EARLY signals (weaker, not sent live):", early)
    out += ["", "Early signals flip often in range-bound markets. Not financial advice."]
    return "\n".join(out)


def market_open() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)


def main():
    print(f"Scanning {INDEX_KEY} every {REFRESH_SECONDS}s (strong-breakout mode).\n")
    fut_key, fut_name = None, None
    try:
        fut_key, fut_name = get_future_key()
    except Exception as e:
        print(f"Futures lookup failed ({e}). Running without volume.")
    print(f"Futures contract: {fut_name}")
    send_telegram(f"Nifty scanner v3 (strong breakouts only) started. "
                  f"Volume source: {fut_name or 'none (volume unverified)'}. "
                  f"Alerts are rare by design.")

    last_candle, last_alert, last_trigger, obs_count = None, None, None, 0
    alerts_sent = []
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
                if len(df) and df.index[-1] != last_candle:  # evaluate each candle once
                    last_candle = df.index[-1]
                    signal, score, why, trig_time = analyze(df, fut)
                    ts, price = df.index[-1].strftime("%H:%M"), df["Close"].iloc[-1]
                    obs = observation(df, fut)
                    print(obs.replace("\n", " | "))
                    print(f"[{ts}] Nifty {price:,.2f} | {signal} | checks passed {abs(score)}")
                    obs_count += 1
                    if OBSERVE_TELEGRAM_EVERY and obs_count % OBSERVE_TELEGRAM_EVERY == 0:
                        send_telegram(obs, silent=True)  # silent: no buzz, breakout alerts still buzz
                    for w in why:
                        print(f"    {w}")
                    cooled = (last_alert is None or
                              (now - last_alert).total_seconds() >= COOLDOWN_MIN * 60)
                    if signal != WAIT and trig_time != last_trigger and cooled:
                        last_alert, last_trigger = now, trig_time
                        alerts_sent.append(f"{signal.split()[1]} {ts}")
                        send_telegram(
                            f"STRONG BREAKOUT: {signal}\nNifty {price:,.2f} at {ts} IST\n"
                            f"Breakout candle: {trig_time.strftime('%H:%M')}\n"
                            + "\n".join(why)
                            + "\n\nNot financial advice. Use your own stop-loss."
                        )
        except requests.RequestException as e:
            print(f"Network/API error: {e}")
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
