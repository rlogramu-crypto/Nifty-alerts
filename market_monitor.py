#!/usr/bin/env python3
"""
market_monitor.py - a SEPARATE scanner for Nifty 50 and Sensex on 15-minute candles (Upstox data -> Telegram alerts).
It places no orders and does not touch nifty_scanner_upstox.py. Educational tool only, not financial advice.

Signal (the same as the Market Monitor README), judged on each COMPLETED 15-minute candle:
                    BUY                              SELL
  RSI (14)          RSI < 40                         RSI > 60
  MACD (12,26,9)    MACD line crosses above signal   MACD line crosses below signal
  EMA9              price above EMA9                 price below EMA9
  EMA50             price above EMA50                price below EMA50
An alert is sent when 3 or more of the 4 agree. (The MACD cross counts only on the candle where it happens.)

Modes
  python market_monitor.py --once        check the latest completed candle of each instrument once, alert, exit.
                                         Run it every 15 minutes (a cron-job.org trigger of monitor.yml, see that file).
  python market_monitor.py --loop        keep running through market hours (server use), checking every 60 s,
                                         with a 15-minute cooldown per instrument.
  python market_monitor.py --backtest    replay the last --days days of Upstox 15-minute candles through the same rules and
                                         report what happened after each alert (also sent to Telegram). Run this BEFORE trusting it.

Secrets (the same names as the Nifty scanner): UPSTOX_ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Every alert is also appended to alerts/monitor_YYYY-MM-DD.csv. There is no Windows desktop notification (this runs on
GitHub or a server); Telegram is the alert channel.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

IST = timezone(timedelta(hours=5, minutes=30))
INSTRUMENTS = {"Nifty 50": "NSE_INDEX|Nifty 50", "Sensex": "BSE_INDEX|SENSEX"}
INTERVAL_MIN = 15
RSI_LEN, RSI_BUY, RSI_SELL = 14, 40.0, 60.0
EMA_FAST, EMA_SLOW = 9, 50
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
MIN_VOTES = 3
COOLDOWN_MIN = 15                    # --loop only: per instrument
MAX_AGE_MIN = 6                      # --once: ignore a candle that closed longer ago than this (stale / duplicate run)
HISTORY_DAYS = 10                    # calendar days of history fetched so EMA50 and MACD are warmed up
MARKET_OPEN, MARKET_CLOSE = (9, 15), (15, 30)
LOG_DIR = os.environ.get("ALERT_LOG_DIR", "alerts")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
WARMUP = EMA_SLOW + 5                # candles needed before any signal counts


# ---------------------------------------------------------------- Telegram
def send_telegram(text, silent=False):
    print(text.replace("\n", " | "))
    if not (TG_TOKEN and TG_CHAT_ID):
        return
    for i in range(0, len(text), 4000):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              json={"chat_id": TG_CHAT_ID, "text": text[i:i + 4000], "disable_notification": silent}, timeout=15)
            if not r.ok:
                print(f"  Telegram error {r.status_code}: {r.text}")
        except requests.RequestException as e:
            print(f"  Telegram send failed: {e}")


# ---------------------------------------------------------------- Upstox data
def _get(url):
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        print("Set the UPSTOX_ACCESS_TOKEN environment variable first.")
        raise SystemExit(3)
    r = requests.get(url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"}, timeout=15)
    if r.status_code == 401:
        send_telegram("Market Monitor: the Upstox token is expired or invalid, so no check was made. Update the token.")
        raise SystemExit(3)
    r.raise_for_status()
    return r.json()["data"]["candles"]            # [timestamp, o, h, l, c, volume, oi], newest first


def _to_df(candles):
    if not candles:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"], index=pd.DatetimeIndex([], tz=IST))
    raw = pd.DataFrame(candles).iloc[:, :5]
    raw.columns = ["Time", "Open", "High", "Low", "Close"]
    raw["Time"] = pd.to_datetime(raw["Time"])
    return raw.sort_values("Time").set_index("Time")


def fetch_history(key, days=HISTORY_DAYS):
    """15-minute candles of the previous days (not today), fetched in windows of at most 25 days."""
    today = datetime.now(IST).date()
    end = today - timedelta(days=1)
    start = today - timedelta(days=days)
    frames = []
    while start <= end:
        chunk_end = min(end, start + timedelta(days=24))
        url = (f"https://api.upstox.com/v3/historical-candle/{quote(key, safe='')}/minutes/{INTERVAL_MIN}/"
               f"{chunk_end.isoformat()}/{start.isoformat()}")
        frames.append(_to_df(_get(url)))
        start = chunk_end + timedelta(days=1)
    return pd.concat(frames) if frames else _to_df([])


def fetch_today(key):
    return _to_df(_get(f"https://api.upstox.com/v3/historical-candle/intraday/{quote(key, safe='')}/minutes/{INTERVAL_MIN}"))


def get_candles(key, now=None):
    """History + today's candles, completed candles only (a candle counts once its 15 minutes are over)."""
    now = now or datetime.now(IST)
    try:
        hist = fetch_history(key)
    except requests.RequestException as e:
        print(f"  history for {key} unavailable ({e}); EMA50 may not be ready")
        hist = _to_df([])
    df = pd.concat([hist, fetch_today(key)])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[df.index + timedelta(minutes=INTERVAL_MIN) <= now]


# ---------------------------------------------------------------- the signal (shared by live, loop and backtest)
def add_signals(df):
    """Adds rsi, macd, macd_sig, ema9, ema50, buy_votes, sell_votes, signal ('BUY' / 'SELL' / '') to a candle frame."""
    d = df.copy()
    c = d["Close"]
    delta = c.diff()
    up, down = delta.clip(lower=0), (-delta).clip(lower=0)
    rs = up.ewm(alpha=1 / RSI_LEN, adjust=False).mean() / down.ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + rs)
    d.loc[down.ewm(alpha=1 / RSI_LEN, adjust=False).mean() == 0, "rsi"] = 100.0
    d["macd"] = c.ewm(span=MACD_FAST, adjust=False).mean() - c.ewm(span=MACD_SLOW, adjust=False).mean()
    d["macd_sig"] = d["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()
    d["ema9"] = c.ewm(span=EMA_FAST, adjust=False).mean()
    d["ema50"] = c.ewm(span=EMA_SLOW, adjust=False).mean()
    d["cross_up"] = (d["macd"].shift(1) <= d["macd_sig"].shift(1)) & (d["macd"] > d["macd_sig"])
    d["cross_down"] = (d["macd"].shift(1) >= d["macd_sig"].shift(1)) & (d["macd"] < d["macd_sig"])
    d["buy_votes"] = (d["rsi"] < RSI_BUY).astype(int) + d["cross_up"].astype(int) + (c > d["ema9"]).astype(int) + (c > d["ema50"]).astype(int)
    d["sell_votes"] = (d["rsi"] > RSI_SELL).astype(int) + d["cross_down"].astype(int) + (c < d["ema9"]).astype(int) + (c < d["ema50"]).astype(int)
    d["signal"] = ""
    d.loc[d["buy_votes"] >= MIN_VOTES, "signal"] = "BUY"
    d.loc[d["sell_votes"] >= MIN_VOTES, "signal"] = "SELL"
    d.iloc[:WARMUP, d.columns.get_loc("signal")] = ""          # indicators are not reliable before they are warmed up
    return d


def alert_text(name, row, ts):
    buy = row["signal"] == "BUY"
    px = float(row["Close"])
    checks = [
        (f"RSI {row['rsi']:.1f} (rule: {'below 40' if buy else 'above 60'})", row["rsi"] < RSI_BUY if buy else row["rsi"] > RSI_SELL),
        (f"MACD crossed {'above' if buy else 'below'} its signal line", bool(row["cross_up"] if buy else row["cross_down"])),
        (f"price {'above' if buy else 'below'} EMA9 ({row['ema9']:,.1f})", px > row["ema9"] if buy else px < row["ema9"]),
        (f"price {'above' if buy else 'below'} EMA50 ({row['ema50']:,.1f})", px > row["ema50"] if buy else px < row["ema50"]),
    ]
    votes = int(row["buy_votes"] if buy else row["sell_votes"])
    lines = [f"{row['signal']}: {name} {px:,.1f}", f"15-min candle {ts:%H:%M}, {votes} of 4 agree"]
    lines += [f"{'yes' if ok else 'no '}: {text}" for text, ok in checks]
    lines.append("Not financial advice.")
    return "\n".join(lines)


def log_alert(name, row, ts):
    path = os.path.join(LOG_DIR, f"monitor_{ts:%Y-%m-%d}.csv")
    os.makedirs(LOG_DIR, exist_ok=True)
    rec = pd.DataFrame([dict(date=f"{ts:%Y-%m-%d}", candle=f"{ts:%H:%M}", instrument=name, signal=row["signal"],
                             price=round(float(row["Close"]), 2), rsi=round(float(row["rsi"]), 1),
                             macd_cross=bool(row["cross_up"] if row["signal"] == "BUY" else row["cross_down"]),
                             ema9=round(float(row["ema9"]), 2), ema50=round(float(row["ema50"]), 2),
                             votes=int(row["buy_votes"] if row["signal"] == "BUY" else row["sell_votes"]))])
    rec.to_csv(path, mode="a", header=not os.path.exists(path), index=False)


def in_market_hours(now):
    return now.weekday() < 5 and MARKET_OPEN <= (now.hour, now.minute) <= MARKET_CLOSE


# ---------------------------------------------------------------- modes
def check_once(max_age=MAX_AGE_MIN, now=None):
    now = now or datetime.now(IST)
    fired = 0
    for name, key in INSTRUMENTS.items():
        try:
            df = get_candles(key, now)
        except requests.RequestException as e:
            print(f"{name}: data error: {e}")
            continue
        if len(df) < WARMUP:
            print(f"{name}: only {len(df)} candles, indicators not ready")
            continue
        d = add_signals(df)
        ts, row = d.index[-1], d.iloc[-1]
        age = (now - (ts + timedelta(minutes=INTERVAL_MIN))).total_seconds() / 60
        if age > max_age:
            print(f"{name}: latest completed candle {ts:%H:%M} closed {age:.0f} min ago, nothing new")
            continue
        if row["signal"]:
            send_telegram(alert_text(name, row, ts))
            log_alert(name, row, ts)
            fired += 1
        else:
            print(f"{name}: candle {ts:%H:%M} no signal (buy {int(row['buy_votes'])}, sell {int(row['sell_votes'])})")
    return fired


def run_loop():
    last_candle, last_alert = {}, {}
    send_telegram("Market Monitor started (Nifty 50 and Sensex, 15-minute candles).", silent=True)
    while True:
        now = datetime.now(IST)
        if now.weekday() >= 5 or (now.hour, now.minute) > MARKET_CLOSE:
            print("Market closed. Stopping.")
            return
        if (now.hour, now.minute) >= MARKET_OPEN:
            for name, key in INSTRUMENTS.items():
                try:
                    df = get_candles(key, now)
                    if len(df) < WARMUP:
                        continue
                    d = add_signals(df)
                    ts, row = d.index[-1], d.iloc[-1]
                    if last_candle.get(name) == ts:
                        continue                                  # this candle was already judged
                    last_candle[name] = ts
                    cooling = name in last_alert and (now - last_alert[name]).total_seconds() < COOLDOWN_MIN * 60
                    if row["signal"] and not cooling:
                        send_telegram(alert_text(name, row, ts))
                        log_alert(name, row, ts)
                        last_alert[name] = now
                except requests.RequestException as e:
                    print(f"{name}: data error: {e}")
        time.sleep(max(5, 60 - datetime.now(IST).second))


# ---------------------------------------------------------------- backtest
HORIZONS = (2, 4, 8)                  # candles after the alert: 30 min, 60 min, 2 hours


def evaluate(frames):
    """frames: {instrument: 15-min candle frame (any number of days)}. Returns (summary text, per-alert DataFrame)."""
    recs, base = [], []
    for name, df in frames.items():
        d = add_signals(df)
        day = d.index.date
        for h in HORIZONS:
            fwd = d["Close"].shift(-h)
            same_day = pd.Series(day, index=d.index).shift(-h) == pd.Series(day, index=d.index)
            d[f"ret{h}"] = np.where(same_day, fwd - d["Close"], np.nan)
        d["valid"] = np.arange(len(d)) >= WARMUP
        for ts, r in d[(d["signal"] != "")].iterrows():
            s = 1 if r["signal"] == "BUY" else -1
            recs.append(dict(instrument=name, time=ts, signal=r["signal"], price=r["Close"],
                             **{f"pts{h}": s * r[f"ret{h}"] for h in HORIZONS},
                             **{f"pct{h}": s * r[f"ret{h}"] / r["Close"] * 100 for h in HORIZONS}))
        v = d[d["valid"]]
        base.append(pd.DataFrame({"instrument": name,
                                  **{f"up{h}": v[f"ret{h}"] for h in HORIZONS}, "close": v["Close"]}))
    A = pd.DataFrame(recs)
    B = pd.concat(base) if base else pd.DataFrame()
    if A.empty:
        return "Market Monitor backtest: no alerts in the period.", A
    ndays = len({t.date() for df in frames.values() for t in df.index})
    out = [f"MARKET MONITOR BACKTEST: {ndays} trading days, move AFTER each alert in the alert's direction (index points, no costs)"]
    for name in frames:
        for sig in ("BUY", "SELL", "ALL"):
            s = A[(A.instrument == name) & ((A.signal == sig) if sig != "ALL" else True)]
            if s.empty:
                continue
            parts = []
            for h in HORIZONS:
                x = s[f"pts{h}"].dropna()
                if len(x):
                    parts.append(f"{h * INTERVAL_MIN}m: {int((x > 0).sum())}/{len(x)} up ({(x > 0).mean():.0%}), avg {x.mean():+.1f}")
            out.append(f"{name} {sig}: {len(s)} alerts ({len(s) / ndays:.1f}/day) | " + " | ".join(parts))
    # what a random candle would have done, so a drifting market is not mistaken for skill
    for name in frames:
        b = B[B.instrument == name]
        if len(b):
            x = b["up4"].dropna()
            out.append(f"{name} for comparison, ANY candle: 60m up {(x > 0).mean():.0%} of the time, avg {x.mean():+.1f} pts (buy-and-hold drift)")
    return "\n".join(out), A


def run_backtest(days):
    frames = {}
    for name, key in INSTRUMENTS.items():
        try:
            frames[name] = fetch_history(key, days)
        except requests.RequestException as e:
            print(f"{name}: history unavailable: {e}")
    frames = {k: v for k, v in frames.items() if len(v) > WARMUP + max(HORIZONS)}
    if not frames:
        send_telegram("Market Monitor backtest: could not get enough history from Upstox.")
        return
    text, A = evaluate(frames)
    send_telegram(text)
    os.makedirs(LOG_DIR, exist_ok=True)
    A.to_csv(os.path.join(LOG_DIR, "monitor_backtest.csv"), index=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Separate Nifty 50 and Sensex 15-minute scanner (Telegram alerts, no orders).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="check the latest completed candle once and exit")
    g.add_argument("--loop", action="store_true", help="run through market hours (server)")
    g.add_argument("--backtest", action="store_true", help="replay recent history and report what followed each alert")
    ap.add_argument("--days", type=int, default=25, help="calendar days of history for --backtest (default 25)")
    ap.add_argument("--max-age", type=float, default=MAX_AGE_MIN, help="--once: ignore a candle older than this many minutes")
    a = ap.parse_args()
    if a.backtest:
        run_backtest(a.days)
    elif a.loop:
        run_loop()
    else:
        now = datetime.now(IST)
        if now.weekday() >= 5 or not (MARKET_OPEN <= (now.hour, now.minute) <= (15, 40)):
            print("Outside market hours, nothing to do.")      # a run at 15:31 is still valid: it judges the 15:15 candle
            sys.exit(0)
        check_once(a.max_age)
