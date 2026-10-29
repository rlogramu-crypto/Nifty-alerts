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


def make_candle_video(src, path="day_candles.mp4", events=None, step=2, fps=12, hold_sec=1.8):
    """Candle replay: price + EMA9/21 + opening range + volume panel. Each alert freezes the video for hold_sec
    with a banner that spells it out. src = CSV path or the day DataFrame."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    df = pd.read_csv(src) if isinstance(src, str) else src.copy()
    df.columns = [c.lower() if c.lower() in ("open", "high", "low", "close") else c for c in df.columns]
    if "Time" in df:
        df["Time"] = pd.to_datetime(df["Time"])
        df = df.set_index("Time")
    df = df.sort_index()
    ev = csv_alert_events(df) if events is None else events
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    n = len(df)
    has_vol = "fut_vol" in df and df["fut_vol"].notna().any()
    vol = df["fut_vol"].fillna(0).values if has_vol else None
    vx = df["vol_x"].fillna(0).values if "vol_x" in df else None
    e9 = df["ema9"].values if "ema9" in df else None
    e21 = df["ema21"].values if "ema21" in df else None
    # VWAP is a futures number: move it onto the Nifty scale by subtracting the futures premium (30-minute median, so it stays smooth)
    vw = None
    if "fut_vwap" in df and "fut_close" in df and df["fut_vwap"].notna().any():
        prem = (df["fut_close"] - df["close"]).rolling(30, min_periods=1).median()
        vw = (df["fut_vwap"] - prem).values
    orh, orl = h[:15].max(), l[:15].min()
    up, dn, ink, mut, grid, bg, amber = "#2fbf71", "#ef5b5b", "#f1efe8", "#9a978d", "#2b2b29", "#161615", "#e8a93a"
    seq = []                                   # (candle index shown, alert number being highlighted or None)
    for k in range(0, n, step):
        seq.append((k, None))
        for m, (i, _, _) in enumerate(ev):
            if k - step < i <= k:
                seq += [(k, m)] * int(fps * hold_sec)
    seq += [(n - 1, "end")] * int(fps * 4)
    fig = plt.figure(figsize=(7.2, 8.0), dpi=120)
    fig.patch.set_facecolor(bg)
    gs = fig.add_gridspec(2 if has_vol else 1, 1, height_ratios=[4, 1] if has_vol else [1], hspace=0.06,
                          left=0.08, right=0.97, top=0.95, bottom=0.075)
    ax = fig.add_subplot(gs[0])
    axv = fig.add_subplot(gs[1], sharex=ax) if has_vol else None
    span = h.max() - l.min()

    fig.text(0.01, 0.01, "OB opening break   BO breakout   PB pullback   TR trend reverse   FB fake breakout   "
             "white = EMA9, amber = EMA21, blue = VWAP", color=mut, fontsize=6.5)

    def style(a_):
        a_.set_facecolor(bg)
        a_.tick_params(colors=ink, labelsize=9)
        a_.grid(color=grid, linewidth=0.5)
        for sp in a_.spines.values():
            sp.set_color(grid)

    def frame(k, m):
        ax.cla()
        style(ax)
        idx = list(range(k + 1))
        col = [up if c[i] >= o[i] else dn for i in idx]
        ax.vlines(idx, l[:k + 1], h[:k + 1], colors=col, linewidth=0.7)
        ax.vlines(idx, [min(o[i], c[i]) for i in idx], [max(o[i], c[i]) + 0.4 for i in idx], colors=col, linewidth=1.6)
        if e9 is not None:
            ax.plot(idx, e9[:k + 1], color=ink, lw=0.8, alpha=0.8, label="EMA9")
            ax.plot(idx, e21[:k + 1], color=amber, lw=0.8, alpha=0.9, label="EMA21")
        if vw is not None:
            ax.plot(idx, vw[:k + 1], color="#4cc9f0", lw=1.3, label="VWAP")
        if k >= 14:
            ax.hlines([orh, orl], 14, k, colors=mut, linestyles="dashed", linewidth=0.8)
            ax.text(k, orh, " opening range", color=mut, fontsize=7, va="bottom", ha="right")
        ylab = {}
        for n_ev, (i, label, sig) in enumerate(ev):
            if i > k:
                continue
            call, code = sig == "CALL", SHORT.get(label, label[:2].upper())
            colr = up if call else dn
            near = [ylab[q] for q, (j, _, s2) in enumerate(ev[:n_ev]) if s2 == sig and abs(j - i) < 10 and q in ylab]
            y = (min([l[i] - 0.045 * span] + [v - 0.04 * span for v in near]) if call
                 else max([h[i] + 0.035 * span] + [v + 0.04 * span for v in near]))
            ylab[n_ev] = y
            ax.plot(i, l[i] - 0.02 * span if call else h[i] + 0.02 * span, "^" if call else "v", color=colr, ms=8)
            ax.text(i, y, f"{code} {df.index[i]:%H:%M}", color=colr, fontsize=8, ha="center", fontweight="bold",
                    va="top" if call else "bottom")
            if m == n_ev:                      # the alert being explained right now: ring + banner
                ax.plot(i, c[i], "o", mfc="none", mec=ink, ms=20, mew=1.8)
        shown = [(i, lb, sg) for i, lb, sg in ev if i <= k]
        if m == "end":
            msg = f"DAY END   {c[-1] - o[0]:+,.0f} pts   alerts marked: {len(ev)}\nOpen {o[0]:,.1f}   High {h.max():,.1f}   Low {l.min():,.1f}   Close {c[-1]:,.1f}"
            colr = ink
        elif isinstance(m, int):
            i, lb, sg = ev[m]
            msg = (f"ALERT {df.index[i]:%H:%M}   {lb.upper()}   {'BUY CALL (CE)' if sg == 'CALL' else 'BUY PUT (PE)'}\n"
                   f"Nifty {c[i]:,.1f}   (stop idea and exit: use your own)")
            colr = up if sg == "CALL" else dn
        elif shown:
            i, lb, sg = shown[-1]
            msg, colr = f"Last alert {df.index[i]:%H:%M}: {lb} {sg}  @ {c[i]:,.1f}", mut
        else:
            msg, colr = "No alert yet", mut
        ax.text(0.01, 0.985, msg, transform=ax.transAxes, color=colr, fontsize=9.5, va="top", fontweight="bold",
                bbox=dict(facecolor=bg, edgecolor=colr, alpha=0.9, boxstyle="round,pad=0.4"))
        xmax = max(k + 25, 80)                     # zooms out as the day grows, so early candles are big
        lo_, hi_ = l[:k + 1].min(), h[:k + 1].max()
        pad = max(hi_ - lo_, 60)
        ax.set_ylim(lo_ - 0.2 * pad, hi_ + 0.3 * pad)
        ax.set_title(f"Nifty {df.index[k]:%d %b %Y  %H:%M}     O {o[k]:,.1f}   H {h[k]:,.1f}   L {l[k]:,.1f}   C {c[k]:,.1f}"
                     f"     day {c[k] - o[0]:+,.0f}", color=ink, fontsize=9.5)
        ticks = list(range(0, min(n, xmax), 30 if xmax <= 200 else 60))
        ax.set_xticks(ticks)
        if has_vol:
            ax.tick_params(labelbottom=False)
            axv.cla()
            style(axv)
            axv.vlines(idx, 0, vol[:k + 1], colors=[amber if (vx is not None and vx[i] >= 1.5) else mut for i in idx], linewidth=1.5)
            axv.set_ylim(0, max(vol.max(), 1) * 1.05)
            axv.set_xticks(ticks)
            axv.set_xticklabels([f"{df.index[i]:%H:%M}" for i in ticks], color=ink, fontsize=9)
            axv.set_xlim(-3, xmax)
            axv.set_ylabel("futures volume\n(amber = 1.5x+)", color=mut, fontsize=7)
            axv.tick_params(labelleft=False)
        else:
            ax.set_xticklabels([f"{df.index[i]:%H:%M}" for i in ticks], color=ink, fontsize=9)
        ax.set_xlim(-3, xmax)

    import subprocess
    fig.canvas.draw()
    w_, h_ = fig.canvas.get_width_height()
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w_}x{h_}",
                           "-r", str(fps), "-i", "-", "-pix_fmt", "yuv420p", "-vcodec", "libx264", "-crf", "24", path],
                          stdin=subprocess.PIPE)
    prev, buf = None, b""
    for key in seq:                            # frozen alert frames are drawn once and written many times
        if key != prev:
            frame(*key)
            fig.canvas.draw()
            buf, prev = bytes(fig.canvas.buffer_rgba()), key
        ff.stdin.write(buf)
    ff.stdin.close()
    ff.wait()
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
                                    "caption": f"Nifty candle replay {day}. Video freezes at each alert. Markers = CSV signal flags, 15 min cooldown. {note}"[:1000]},
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


def window_texts(t: pd.DataFrame, a: str, b: str, per_msg: int = 60, max_msgs: int = 8) -> list:
    """The window as several Telegram messages of per_msg rows each (one message holds about 60 rows)."""
    w = t.between_time(a, b)
    if w.empty:
        return [f"No rows between {a} and {b}."]
    num = lambda v, f: "-" if pd.isna(v) else format(v, f)
    head = "time  close  chg  futvol  vol_x  vs_vwap  rsi  sig"
    rows = []
    for ts, r in w.iterrows():
        sig = ("S-" + r["strong"]) if r["strong"] else ("w-" + r["weak_trend"]) if r["weak_trend"] else ""
        vx = num(r.get("vol_x", float("nan")), ".1f")
        rows.append(f"{ts:%H:%M} {r['close']:.1f} {r['chg']:+.1f} "
                    f"{num(r.get('fut_vol', float('nan')), '.0f')} {vx + 'x' if vx != '-' else '-'} "
                    f"{num(r.get('fut_vs_vwap', float('nan')), '+.0f')} {num(r['rsi14'], '.0f')} {sig}")
    parts = [head + "\n" + "\n".join(rows[i:i + per_msg]) for i in range(0, len(rows), per_msg)]
    if len(parts) > max_msgs:
        parts = parts[:max_msgs]
        parts[-1] += f"\n(stopped after {max_msgs * per_msg} rows; the CSV has all of them)"
    return parts


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
        parts = window_texts(t, a, b)
        for n_, part in enumerate(parts, 1):
            send_telegram(f"{day} {a}-{b}  ({n_}/{len(parts)})\n" + part)
    send_day_video(t, day, fut_note)            # last, so a video problem never blocks the CSV or text


if __name__ == "__main__":
    main()
