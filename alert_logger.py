"""
alert_logger.py - forward-test log for the Nifty scanner (use with nifty_scanner_v9_3.py).

What it does
  1. log_alert():   called by the scanner every time a live CALL/PUT alert is sent. Appends one row to that
                    day's own file alerts/alerts_YYYY-MM-DD.csv (entry, stop, target, context, and how many
                    seconds after the candle closed the alert was created). One CSV per trading day.
  2. resolve_day(): called once after 15:30. Fills in what happened next, from the day's 1-minute candles.
  3. tally():       text summary by alert type (win rate with a 95% range, last-30 rolling rate, and the same
                    test with a 1- and 2-candle late entry). Appended to the end-of-day Telegram message.

How an outcome is decided (same idea as your replay: +risk before -risk within 60 minutes)
  W = target touched first, L = stop touched first, U = neither within 60 minutes (or the day ended).
  If one candle touches BOTH levels the order inside the candle is unknown, so it is counted as L (the
  conservative choice) and flagged in the *_ambig column.
  fixed_*  : +/-RISK_PTS around the entry (30 pts), directly comparable with the +30/-30 numbers in the scanner.
  own_*    : the alert's own stop and a 1R target (what the SUMMARY line at the top of the alert shows).
  d1_*/d2_*: the fixed test, but entering at the CLOSE of the 1st / 2nd candle after the signal candle.
             (An index candle opens almost exactly at the previous close, so "next open" is not a real delay;
             a later close is.)

Index points only: no spread, brokerage, STT or option decay. Subtract your own cost estimate.
"""
import math
import os

import numpy as np
import pandas as pd

import glob

LOG_DIR = os.environ.get("ALERT_LOG_DIR", "alerts")      # one CSV per day: alerts/alerts_2026-11-19.csv
HORIZON_MIN = 60
RISK_PTS = 30
BASELINE = {"OPENING BREAK": 60, "BREAKOUT": 67, "PULLBACK": 70}   # win % from the v9.1 replay; SWING has no comparable figure

COLS = ["date", "candle_time", "sent_at", "latency_s", "kinds", "signal", "entry", "stop", "target", "risk_pts",
        "ctx_for", "ctx_against",
        "fixed_result", "fixed_min", "fixed_ambig", "own_result", "own_min", "own_ambig",
        "d1_result", "d2_result", "net60", "mfe60", "mae60"]


def wilson(wins, n, z=1.96):
    """95% range for a win rate (Wilson interval). Returns (low, high) as fractions."""
    if n <= 0:
        return 0.0, 0.0
    p = wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return centre - half, centre + half


def first_touch(d, start, sign, stop, target, horizon=HORIZON_MIN):
    """Walk the candles d.iloc[start : start+horizon]. Returns (result, minutes, ambiguous).
    result: 'W' target first, 'L' stop first (or both in one candle), 'U' neither."""
    w = d.iloc[start:start + horizon]
    for k, (hi, lo) in enumerate(zip(w["High"].to_numpy(), w["Low"].to_numpy())):
        hit_t = hi >= target if sign > 0 else lo <= target
        hit_s = lo <= stop if sign > 0 else hi >= stop
        if hit_t and hit_s:
            return "L", k + 1, True
        if hit_s:
            return "L", k + 1, False
        if hit_t:
            return "W", k + 1, False
    return "U", len(w), False


def fixed_outcome(d, i, sign, entry, risk=RISK_PTS, horizon=HORIZON_MIN):
    """+/-risk test with the entry at `entry`, looking at candles after index i."""
    return first_touch(d, i + 1, sign, entry - sign * risk, entry + sign * risk, horizon)


def day_path(day, log_dir=None):
    """File that holds all alerts of one day (day = 'YYYY-MM-DD')."""
    return os.path.join(log_dir or LOG_DIR, f"alerts_{day}.csv")


def _read(path):
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for c in COLS:
        if c not in df.columns:
            df[c] = ""
    return df[COLS]


def levels_for(grp, price, d, risk_pts):
    """Same entry / stop / target the SUMMARY line shows (an alert with its own stop uses it, otherwise +/-risk_pts)."""
    own = next((s for s in grp if "stop" in s and "entry" in s), None)
    entry = float(own["entry"]) if own else float(price)
    stop = float(own["stop"]) if own else entry - d * risk_pts
    risk = abs(entry - stop)
    return entry, stop, entry + d * risk, risk


def log_alert(candle_time, sent_at, grp, d, price, risk_pts=RISK_PTS, pro=(), con=(), path=None):
    """Append one live alert. d = +1 CALL / -1 PUT. Never raises into the scanner's loop (the caller wraps it too).
    Skips an exact duplicate (same day, candle, kinds and direction), e.g. after a restart."""
    path = path or day_path(f"{candle_time:%Y-%m-%d}")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    entry, stop, target, risk = levels_for(grp, price, d, risk_pts)
    kinds = " + ".join(s["kind"] for s in grp)
    latency = (sent_at - (candle_time + pd.Timedelta(minutes=1))).total_seconds()
    row = {c: "" for c in COLS}
    row.update(date=f"{candle_time:%Y-%m-%d}", candle_time=f"{candle_time:%H:%M}", sent_at=f"{sent_at:%H:%M:%S}",
               latency_s=f"{latency:.0f}", kinds=kinds, signal="CALL" if d > 0 else "PUT",
               entry=f"{entry:.2f}", stop=f"{stop:.2f}", target=f"{target:.2f}", risk_pts=f"{risk:.1f}",
               ctx_for="; ".join(pro), ctx_against="; ".join(con))
    cur = _read(path)
    dup = ((cur["date"] == row["date"]) & (cur["candle_time"] == row["candle_time"])
           & (cur["kinds"] == kinds) & (cur["signal"] == row["signal"])).any()
    if dup:
        return False
    pd.DataFrame([row], columns=COLS).to_csv(path, mode="a", header=not os.path.exists(path), index=False)
    return True


def resolve_day(df, path=None, day=None):
    """Fill the outcome columns for `day` (default: the last day in df) from its 1-minute candles. Returns rows resolved."""
    if df is None or not len(df):
        return 0
    day = day or f"{df.index[-1]:%Y-%m-%d}"
    path = path or day_path(day)
    log = _read(path)
    if log.empty:
        return 0
    d = df[[f"{t:%Y-%m-%d}" == day for t in df.index]]
    n_done = 0
    for idx in log.index[(log["date"] == day) & (log["fixed_result"] == "")]:
        r = log.loc[idx]
        t = pd.Timestamp(f"{day} {r['candle_time']}")
        t = t.tz_localize(d.index.tz) if d.index.tz is not None and t.tzinfo is None else t
        if t not in d.index:
            continue
        i = d.index.get_loc(t)
        sign = 1 if r["signal"] == "CALL" else -1
        entry, stop, target = float(r["entry"]), float(r["stop"]), float(r["target"])
        close = float(d["Close"].iloc[i])
        res, mins, amb = fixed_outcome(d, i, sign, close)
        log.loc[idx, ["fixed_result", "fixed_min", "fixed_ambig"]] = [res, str(mins), "1" if amb else ""]
        res, mins, amb = first_touch(d, i + 1, sign, stop, target)
        log.loc[idx, ["own_result", "own_min", "own_ambig"]] = [res, str(mins), "1" if amb else ""]
        for k in (1, 2):
            col = f"d{k}_result"
            if i + k < len(d):
                log.loc[idx, col] = fixed_outcome(d, i + k, sign, float(d["Close"].iloc[i + k]))[0]
        w = d.iloc[i + 1: i + 1 + HORIZON_MIN]
        if len(w):
            best = (w["High"].max() - entry) if sign > 0 else (entry - w["Low"].min())
            worst = (entry - w["Low"].min()) if sign > 0 else (w["High"].max() - entry)
            log.loc[idx, ["net60", "mfe60", "mae60"]] = [f"{sign * (w['Close'].iloc[-1] - entry):.1f}",
                                                          f"{max(0.0, best):.1f}", f"{max(0.0, worst):.1f}"]
        n_done += 1
    if n_done:
        log.to_csv(path, index=False)
    return n_done


def _rate(rows, col):
    dec = rows[rows[col].isin(["W", "L"])]
    w = int((dec[col] == "W").sum())
    return w, len(dec), int((rows[col] == "U").sum())


def tally(path=None, last_n=30):
    """Forward-test summary text (empty string if nothing is resolved yet)."""
    files = [path] if path else sorted(glob.glob(os.path.join(LOG_DIR, "alerts_*.csv")))
    frames = [_read(p) for p in files]
    log = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS)
    log = log[log["fixed_result"] != ""]
    if log.empty:
        return ""
    out = [f"FORWARD TEST ({len(files)} day files in {LOG_DIR}/): {len(log)} resolved alerts, {log['date'].iloc[0]} to {log['date'].iloc[-1]}",
           "Win = +30 before -30 within 60 min, index points, entry at the signal close. No costs."]
    kinds = sorted({k for ks in log["kinds"] for k in ks.split(" + ")})
    for kind in kinds + ["ALL"]:
        rows = log if kind == "ALL" else log[log["kinds"].apply(lambda s: kind in s.split(" + "))]
        w, n, u = _rate(rows, "fixed_result")
        if not n:
            continue
        lo, hi = wilson(w, n)
        line = f"{kind}: {w}/{n} won = {w / n:.0%} (95% range {lo:.0%}-{hi:.0%})" + (f", {u} undecided" if u else "")
        base = BASELINE.get(kind)
        if base is not None:
            line += f" | replay said {base}%"
        if n < 30:
            line += " | too few to judge"
        out.append(line)
        if kind == "ALL":
            for k in (1, 2):
                w2, n2, _ = _rate(rows, f"d{k}_result")
                if n2:
                    out.append(f"  entering {k} candle{'s' if k > 1 else ''} late: {w2}/{n2} = {w2 / n2:.0%}")
            w3, n3, _ = _rate(rows.tail(last_n), "fixed_result")
            if n3:
                out.append(f"  last {min(last_n, len(rows))} alerts: {w3}/{n3} = {w3 / n3:.0%}")
            lat = pd.to_numeric(rows["latency_s"], errors="coerce").dropna()
            if len(lat):
                out.append(f"  alert latency after candle close: median {lat.median():.0f}s, worst {lat.max():.0f}s "
                           "(before Telegram delivery and your own reaction)")
            amb = int((rows["fixed_ambig"] == "1").sum())
            if amb:
                out.append(f"  {amb} alerts touched both levels in one candle (counted as losses)")
    return "\n".join(out)


if __name__ == "__main__":      # python alert_logger.py  -> print the summary of the current log
    print(tally() or "No resolved alerts yet.")
