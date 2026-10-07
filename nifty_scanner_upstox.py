"""
Nifty 50 1-minute pattern scanner using the Upstox API -> CALL / PUT / WAIT.
Educational tool only. Not financial advice.

Setup:
  pip install requests pandas
  Get your daily access token from your Upstox developer app, then:
    Windows (cmd):   set UPSTOX_ACCESS_TOKEN=your_token
    Mac/Linux:       export UPSTOX_ACCESS_TOKEN=your_token
  Telegram alerts (optional but recommended):
    1. In Telegram, message @BotFather -> /newbot -> copy the bot token.
    2. Open a chat with your new bot and send it any message (e.g. "hi").
    3. Open https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id": ...}
    4. export TELEGRAM_BOT_TOKEN=...   and   export TELEGRAM_CHAT_ID=...
  python nifty_scanner_upstox.py

Notes:
  - Access tokens expire daily, so generate a fresh one each morning.
  - Uses Upstox intraday candle API V3: /v3/historical-candle/intraday/{key}/minutes/1
  - Instrument key is case-sensitive: "NSE_INDEX|Nifty 50"
  - Index volume is often 0, in which case the VWAP check is skipped.
"""
import os
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import pandas as pd
import requests

INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
URL = f"https://api.upstox.com/v3/historical-candle/intraday/{quote(INSTRUMENT_KEY, safe='')}/minutes/1"
REFRESH_SECONDS = 60
OPENING_RANGE_MIN = 15
MIN_SCORE = 3
IST = timezone(timedelta(hours=5, minutes=30))
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(text: str) -> None:
    if not (TG_TOKEN and TG_CHAT_ID):
        print("  (Telegram not configured, skipping alert)")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=10,
        )
        if not r.ok:
            print(f"  Telegram error {r.status_code}: {r.text}")
    except requests.RequestException as e:
        print(f"  Telegram send failed: {e}")


def fetch_candles() -> pd.DataFrame:
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise SystemExit("Set the UPSTOX_ACCESS_TOKEN environment variable first.")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    r = requests.get(URL, headers=headers, timeout=10)
    if r.status_code == 401:
        raise SystemExit("Token expired or invalid. Generate a new Upstox access token.")
    r.raise_for_status()
    candles = r.json()["data"]["candles"]  # [ts, open, high, low, close, volume, oi], newest first
    df = pd.DataFrame(candles).iloc[:, :6]
    df.columns = ["Time", "Open", "High", "Low", "Close", "Volume"]
    df["Time"] = pd.to_datetime(df["Time"])
    df = df.sort_values("Time").set_index("Time")
    # Drop the still-forming candle so signals use closed candles only
    now_min = datetime.now(IST).replace(second=0, microsecond=0)
    if len(df) and df.index[-1].to_pydatetime() >= now_min:
        df = df.iloc[:-1]
    return df


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, 1e-9))


def analyze(df: pd.DataFrame):
    if len(df) < 25:
        return "WAIT", 0, ["Not enough candles yet (need ~25)."]

    c = df["Close"]
    df = df.copy()
    df["ema9"] = c.ewm(span=9, adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()
    df["rsi"] = rsi(c)

    last, prev = df.iloc[-1], df.iloc[-2]
    score, why = 0, []

    # 1. EMA trend / crossover
    if last.ema9 > last.ema21:
        score += 1; why.append("EMA9 above EMA21 (bullish)")
        if prev.ema9 <= prev.ema21:
            score += 1; why.append("Fresh bullish EMA crossover")
    else:
        score -= 1; why.append("EMA9 below EMA21 (bearish)")
        if prev.ema9 >= prev.ema21:
            score -= 1; why.append("Fresh bearish EMA crossover")

    # 2. VWAP (only if volume exists; index data often has 0 volume)
    if df["Volume"].sum() > 0:
        tp = (df["High"] + df["Low"] + df["Close"]) / 3
        vwap = (tp * df["Volume"]).cumsum() / df["Volume"].cumsum()
        if last.Close > vwap.iloc[-1]:
            score += 1; why.append("Price above VWAP")
        else:
            score -= 1; why.append("Price below VWAP")

    # 3. Opening range breakout
    orng = df.iloc[:OPENING_RANGE_MIN]
    if len(df) > OPENING_RANGE_MIN:
        if last.Close > orng["High"].max():
            score += 1; why.append("Broke above opening range high")
        elif last.Close < orng["Low"].min():
            score -= 1; why.append("Broke below opening range low")

    # 4. RSI momentum (avoid chasing extremes)
    if 55 < last.rsi < 70:
        score += 1; why.append(f"RSI {last.rsi:.0f} bullish momentum")
    elif 30 < last.rsi < 45:
        score -= 1; why.append(f"RSI {last.rsi:.0f} bearish momentum")
    elif last.rsi >= 70 or last.rsi <= 30:
        why.append(f"RSI {last.rsi:.0f} extreme: reversal risk, don't chase")

    # 5. Engulfing candle
    p_body = prev.Close - prev.Open
    l_body = last.Close - last.Open
    if p_body < 0 < l_body and last.Close > prev.Open and last.Open < prev.Close:
        score += 1; why.append("Bullish engulfing candle")
    elif p_body > 0 > l_body and last.Close < prev.Open and last.Open > prev.Close:
        score -= 1; why.append("Bearish engulfing candle")

    if score >= MIN_SCORE:
        signal = "BUY CALL (CE)"
    elif score <= -MIN_SCORE:
        signal = "BUY PUT (PE)"
    else:
        signal = "WAIT / NO TRADE"
    return signal, score, why


def market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) <= (15, 30)


def main():
    print(f"Scanning {INSTRUMENT_KEY} every {REFRESH_SECONDS}s. Ctrl+C to stop.\n")
    send_telegram("Nifty scanner started.")
    last_signal = None
    while True:
        try:
            now = datetime.now(IST)
            if now.weekday() < 5 and (now.hour, now.minute) > (15, 30):
                send_telegram("Market closed. Scanner stopped for the day.")
                print("Market closed. Stopping.")
                break
            if not market_open():
                print("Waiting for market open (9:15 IST, Mon-Fri)...")
            else:
                df = fetch_candles()
                signal, score, why = analyze(df)
                ts = df.index[-1].strftime("%H:%M") if len(df) else "--"
                price = df["Close"].iloc[-1] if len(df) else float("nan")
                print(f"[{ts}] Nifty {price:,.2f} | Score {score:+d} | {signal}")
                for w in why:
                    print(f"    - {w}")
                print()
                # Alert only when the signal changes to CALL or PUT (no spam)
                if signal != last_signal:
                    if signal != "WAIT / NO TRADE":
                        send_telegram(
                            f"{signal}\nNifty {price:,.2f} at {ts} IST\nScore {score:+d}\n"
                            + "\n".join(f"- {w}" for w in why)
                            + "\n\nNot financial advice. Use your own stop-loss."
                        )
                    elif last_signal is not None:
                        send_telegram(f"Signal cleared: WAIT / NO TRADE (Nifty {price:,.2f} at {ts})")
                    last_signal = signal
        except requests.RequestException as e:
            print(f"Network/API error: {e}")
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
