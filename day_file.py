"""
Fetch ONE past day of Nifty 1-minute data from Upstox and send it to Telegram as a CSV,
plus an optional compact text table for a time window, plus a candlestick replay video.
Educational only. Not financial advice. Places NO orders.

Runs by itself at 15:40 IST on weekdays (today's data), or by hand: Actions > Day data file > Run workflow.
Env: UPSTOX_ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATE, FROM_TIME, TO_TIME
Needs: matplotlib in requirements.txt and ffmpeg installed in the workflow (for the video only).

CSV columns (one row per minute):
  open/high/low/close (Nifty), chg, fut_close, fut_vol, vol_x (vs prior 20 min avg),
  fut_vwap (running day VWAP), fut_vs_vwap, ema9/ema21/ema50, rsi14,
  vs_or_high / vs_or_low (points beyond the 15-min opening range),
  indication, strong (BREAKOUT), fake, pullback, reverse, opening_break = CALL/PUT on every minute the condition was true.
  weak_trend = a rough trend score (never sent as an alert; it is ON for most of a trend day)
  (raw, before the 15-minute cooldown, so you will see runs of the same signal)
"""
import os
import sys
from datetime import datetime, timedelta
from urllib.parse import quote

import pandas as pd
import requests

from nifty_scanner_upstox import (CALL, IST, INDEX_KEY, OPENING_RANGE_MIN, PUT, WAIT, TG_CHAT_ID,
                                  TG_TOKEN, IND_MIN_CANDLES, early_signal, fetch_candles, get_future_key, rsi,
                                  scan_candle, send_telegram)


# ---------------------------------------------------------------- candle video
VIDEO_KINDS = (("opening_break", "Opening break"), ("strong", "Breakout"), ("fake", "Fake breakout"),
               ("pullback", "Pullback"), ("reverse", "Trend reverse"))
SHORT = {"Opening break": "OB", "Breakout": "BO", "Fake breakout": "FB", "Pullback": "PB", "Trend reverse": "TR"}
VIDEO_COOLDOWN_MIN = 15
VIDEO_LAST_ALERT = (15, 15)      # no alert marker after this time (use (13, 30) to match the old cutoff)


def csv_alert_events(df):
    """[(row index, label, 'CALL'/'PUT')] from the CSV flag columns; first flag per run, cooldown per type."""
    out = []
    for col, label in VIDEO_KINDS:
        if col not in df:
            continue
        last = None
        for i, v in enumerate(df[col]):
            if v not in ("CALL", "PUT"):
                continue
            t = df.index[i]
            if (t.hour, t.minute) >= VIDEO_LAST_ALERT:
                continue
            if last is not None and (t - last).total_seconds() < VIDEO_COOLDOWN_MIN * 60:
                continue
            last = t
            out.append((i, label, v))
    return sorted(out)


def make_candle_video(src, path="day_candles.mp4", events=None, step=5, fps=15):
    """src = CSV path or DataFrame with Time/open/high/low/close. events = optional [(row index, label, 'CALL'/'PUT')]."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation, FFMpegWriter
    df = pd.read_csv(src) if isinstance(src, str) else src.copy()
    df.columns = [c.lower() if c.lower() in ("open", "high", "low", "close") else c for c in df.columns]
    if "Time" in df:
        df["Time"] = pd.to_datetime(df["Time"])
        df = df.set_index("Time")
    df = df.sort_index()
    ev = csv_alert_events(df) if events is None else events
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    n = len(df)
    up, dn, ink, grid, bg = "#1a8a4a", "#c93c3c", "#e8e6df", "#2b2b29", "#161615"
    fig, ax = plt.subplots(figsize=(7.2, 4.05), dpi=100)
    fig.patch.set_facecolor(bg)

    def frame(f):
        k = min(n - 1, f * step)
        ax.cla()
        ax.set_facecolor(bg)
        idx = range(k + 1)
        col = [up if c[i] >= o[i] else dn for i in idx]
        ax.vlines(list(idx), h[:k + 1], l[:k + 1], colors=col, linewidth=0.8)
        ax.bar(list(idx), [abs(c[i] - o[i]) or 0.5 for i in idx], bottom=[min(o[i], c[i]) for i in idx],
               color=col, width=0.7)
        for i, label, sig in ev:
            if i <= k:
                call, code = sig == "CALL", SHORT.get(label, label[:2].upper())
                colr = up if call else dn
                ax.plot(i, l[i] - 8 if call else h[i] + 8, "^" if call else "v", color=colr, ms=6)
                ax.text(i, l[i] - 20 if call else h[i] + 12, code, color=colr, fontsize=7, ha="center",
                        fontweight="bold", va="top" if call else "bottom")
        ax.text(0.01, 0.02, "OB opening break  BO breakout  PB pullback  TR trend reverse  FB fake breakout",
                transform=ax.transAxes, color=ink, fontsize=6)
        ax.set_xlim(-2, n + 2)
        ax.set_ylim(l.min() - 40, h.max() + 40)
        ticks = [i for i in range(0, n, 60)]
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{df.index[i]:%H:%M}" for i in ticks], color=ink, fontsize=7)
        ax.tick_params(axis="y", colors=ink, labelsize=7)
        ax.grid(color=grid, linewidth=0.5)
        for s in ax.spines.values():
            s.set_color(grid)
        ax.set_title(f"Nifty {df.index[k]:%d %b %Y  %H:%M}   O {o[k]:,.1f}  H {h[k]:,.1f}  L {l[k]:,.1f}  C {c[k]:,.1f}",
                     color=ink, fontsize=8)
        fig.tight_layout()

    FuncAnimation(fig, frame, frames=n // step + 1 + fps * 2).save(path, writer=FFMpegWriter(fps=fps))
    plt.close(fig)
    return path


def send_day_video(table, day, note=""):
    """Build the candle video from the day table and send it. Never raises, so the CSV and text always go out first."""
    try:
        path = make_candle_video(table, f"nifty_{day}.mp4")
        if not (TG_TOKEN and TG_CHAT_ID):
            print("Telegram not configured; video saved at", path)
            return
        with open(path, "rb") as fh:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendVideo",
                              data={"chat_id": TG_CHAT_ID, "supports_streaming": "true",
                                    "caption": f"Nifty candle replay {day}. Markers = the CSV signal flags, 15 min cooldown. {note}"[:1000]},
                              files={"video": fh}, timeout=180)
        if not r.ok:
            print(f"Telegram video error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"Day video failed: {e}")


def fetch_day(key: str, day: str) -> pd.DataFrame:
    """1-minute candles for one date. Today comes from the intraday endpoint; earlier days
    come from the V3 historical endpoint (which only serves finished days)."""
    if day == datetime.now(IST).strftime("%Y-%m-%d"):
        df = fetch_candles(key)
        return df[df.index.strftime("%Y-%m-%d") == day].between_time("09:15", "15:30")
    headers = {"Accept": "application/json",
               "Authorization": f"Bearer {os.environ['UPSTOX_ACCESS_TOKEN']}"}
    nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    for to_date in (day, nxt):  # some instruments only answer with the next day as to_date
        url = (f"https://api.upstox.com/v3/historical-candle/{quote(key, safe='')}"
               f"/minutes/1/{to_date}/{day}")
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 401:
            raise SystemExit("Token expired or invalid. Generate a new Upstox access token.")
        r.raise_for_status()
        candles = r.json().get("data", {}).get("candles", [])
        if candles:
            df = pd.DataFrame(candles).iloc[:, :6]
            df.columns = ["Time", "Open", "High", "Low", "Close", "Volume"]
            df["Time"] = pd.to_datetime(df["Time"])
            df = df.sort_values("Time").set_index("Time")
            df = df[df.index.strftime("%Y-%m-%d") == day].between_time("09:15", "15:30")
            if len(df):
                return df
    return pd.DataFrame()


def build_table(df: pd.DataFrame, fut) -> pd.DataFrame:
    t = pd.DataFrame(index=df.index)
    t["open"], t["high"], t["low"], t["close"] = df["Open"], df["High"], df["Low"], df["Close"]
    t["chg"] = df["Close"] - df["Open"]
    if fut is not None and len(fut):
        f = fut.reindex(df.index)
        t["fut_close"] = f["Close"]
        t["fut_vol"] = f["Volume"]
        base = f["Volume"].shift(1).rolling(20, min_periods=5).mean()
        t["vol_x"] = f["Volume"] / base
        tp = (f["High"] + f["Low"] + f["Close"]) / 3
        pv = (tp * f["Volume"].fillna(0)).cumsum()
        t["fut_vwap"] = pv / f["Volume"].fillna(0).cumsum().replace(0, float("nan"))
        t["fut_vs_vwap"] = f["Close"] - t["fut_vwap"]
    c = df["Close"]
    t["ema9"] = c.ewm(span=9, adjust=False).mean()
    t["ema21"] = c.ewm(span=21, adjust=False).mean()
    t["ema50"] = c.ewm(span=50, adjust=False).mean()
    t["rsi14"] = rsi(c)
    orng = df.iloc[:OPENING_RANGE_MIN]
    t["vs_or_high"] = df["Close"] - orng["High"].max()
    t["vs_or_low"] = df["Close"] - orng["Low"].min()
    t.loc[t.index[:OPENING_RANGE_MIN], ["vs_or_high", "vs_or_low"]] = float("nan")
    names = {"INDICATION": "indication", "BREAKOUT": "strong", "FAKE BREAKOUT": "fake", "PULLBACK": "pullback",
             "TREND REVERSE": "reverse", "OPENING BREAK": "opening_break"}
    cols = {k: [] for k in ["weak_trend"] + list(names.values())}
    for i in range(len(df)):
        d = df.iloc[: i + 1]
        f = fut[fut.index <= d.index[-1]] if fut is not None and len(fut) else None
        e = early_signal(d, f)
        cols["weak_trend"].append(e.split()[1] if e != WAIT else "")
        found = {v: "" for v in names.values()}
        if i >= IND_MIN_CANDLES - 1:
            for sg in scan_candle(d, f)[0]:
                found[names[sg["kind"]]] = sg["signal"].split()[1]
        for k, v in found.items():
            cols[k].append(v)
    for k, v in cols.items():
        t[k] = v
    return t.round(2)


def window_text(t: pd.DataFrame, a: str, b: str) -> str:
    w = t.between_time(a, b)
    if w.empty:
        return f"No rows between {a} and {b}."
    rows = ["time  close  chg  futvol  vol_x  vs_vwap  rsi  sig"]
    for ts, r in w.iterrows():
        sig = ("S-" + r["strong"]) if r["strong"] else ("w-" + r["weak_trend"]) if r["weak_trend"] else ""
        rows.append(f"{ts:%H:%M} {r['close']:.1f} {r['chg']:+.1f} "
                    f"{r.get('fut_vol', float('nan')):.0f} {r.get('vol_x', float('nan')):.1f}x "
                    f"{r.get('fut_vs_vwap', float('nan')):+.0f} {r['rsi14']:.0f} {sig}")
    return "\n".join(rows[:61])  # keeps the message under Telegram's size limit


def send_document(path: str, caption: str) -> None:
    if not (TG_TOKEN and TG_CHAT_ID):
        print("Telegram not configured.")
        return
    with open(path, "rb") as fh:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                          data={"chat_id": TG_CHAT_ID, "caption": caption},
                          files={"document": fh}, timeout=60)
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text}")


def main():
    day = (os.environ.get("DATE") or datetime.now(IST).strftime("%Y-%m-%d")).strip()
    a, b = os.environ.get("FROM_TIME", "").strip(), os.environ.get("TO_TIME", "").strip()
    print(f"Fetching {day} ...")
    df = fetch_day(INDEX_KEY, day)
    if df.empty:
        send_telegram(f"No Nifty candles found for {day}. Check the date (trading day, within the last ~month).")
        raise SystemExit("No data.")
    fut = None
    try:
        key, name, _expiry = get_future_key()   # v7 returns 3 values (the old 2-value unpack failed and dropped all futures columns)
        fut = fetch_day(key, day) if key else None
        fut_note = f"futures {name}" if fut is not None and len(fut) else "futures data not found"
    except Exception as e:
        fut_note = f"futures lookup failed ({e})"
    t = build_table(df, fut)
    path = f"nifty_{day}.csv"
    t.to_csv(path)
    send_document(path, f"Nifty 1-minute data {day} ({len(t)} rows, {fut_note})")
    if a and b:
        send_telegram(f"{day} {a}-{b}\n" + window_text(t, a, b))
    send_day_video(t, day, fut_note)            # last, so a video problem never blocks the CSV or text


if __name__ == "__main__":
    main()
