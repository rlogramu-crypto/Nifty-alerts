"""
Month-end report: reads every saved day CSV (data/nifty_YYYY-MM-DD.csv) for one month, rebuilds the alerts from
the CSV signal flags (15 minute cooldown per type, like the scanner), measures how each one did, and sends
a summary + the combined CSV + the alerts list to Telegram. Places NO orders. Not financial advice.
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MONTH (YYYY-MM, blank = this month), FORCE (yes = run even if not month end)
"""
import glob
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

IST = timezone(timedelta(hours=5, minutes=30))
KINDS = (("opening_break", "OPENING BREAK"), ("strong", "BREAKOUT"), ("fake", "FAKE BREAKOUT"),
         ("pullback", "PULLBACK"), ("reverse", "TREND REVERSE"))
COOLDOWN_MIN = 15
HOLD = 60                  # minutes each alert is followed
OB_EXIT = (15, 15)
LATE_FROM = (13, 30)       # alerts from this time are also reported separately
BUCKETS = (("09:15-10:30", (9, 15), (10, 30)), ("10:30-12:00", (10, 30), (12, 0)),
           ("12:00-13:30", (12, 0), (13, 30)), ("13:30-15:15", (13, 30), (15, 15)))
MIN_DAYS_TRUST = 20
TG_TOKEN, TG_CHAT = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def tg_text(text):
    if not (TG_TOKEN and TG_CHAT):
        print(text)
        return
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text}, timeout=15)
    if not r.ok:
        print("Telegram error", r.status_code, r.text)


def tg_file(path, caption):
    if not (TG_TOKEN and TG_CHAT):
        print("saved", path)
        return
    with open(path, "rb") as fh:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                          data={"chat_id": TG_CHAT, "caption": caption}, files={"document": fh}, timeout=60)
    if not r.ok:
        print("Telegram error", r.status_code, r.text)


def load_day(path):
    df = pd.read_csv(path)
    df["Time"] = pd.to_datetime(df["Time"])
    return df.set_index("Time").sort_index()


def day_alerts(df, day):
    """One row per alert: first flag of a run, cooldown per type, result 60 min later (opening break: held to 15:15, stop at range middle)."""
    rows = []
    orh, orl = df["high"].iloc[:15].max(), df["low"].iloc[:15].min()
    for col, kind in KINDS:
        if col not in df:
            continue
        last = None
        for i, v in enumerate(df[col]):
            if v not in ("CALL", "PUT"):
                continue
            t = df.index[i]
            if last is not None and (t - last).total_seconds() < COOLDOWN_MIN * 60:
                continue
            last = t
            sign = 1 if v == "CALL" else -1
            entry = float(df["close"].iloc[i])
            w = df.iloc[i + 1:i + 1 + HOLD]
            if w.empty:
                continue
            best = max(0.0, sign * ((w["high"].max() if sign > 0 else w["low"].min()) - entry))
            worst = max(0.0, -sign * ((w["low"].min() if sign > 0 else w["high"].max()) - entry))
            net = sign * (float(w["close"].iloc[-1]) - entry)
            how = f"{HOLD}m"
            if kind == "OPENING BREAK":
                stop, net, how = (orh + orl) / 2, None, "15:15"
                for j in range(i + 1, len(df)):
                    ts = df.index[j]
                    if (sign > 0 and df["low"].iloc[j] <= stop) or (sign < 0 and df["high"].iloc[j] >= stop):
                        net, how = sign * (stop - entry), f"stopped {ts:%H:%M}"
                        break
                    if (ts.hour, ts.minute) >= OB_EXIT:
                        net = sign * (float(df["close"].iloc[j]) - entry)
                        break
                if net is None:
                    net = sign * (float(df["close"].iloc[-1]) - entry)
            vg = df["fut_vs_vwap"].iloc[i] if "fut_vs_vwap" in df else float("nan")
            vx = df["vol_x"].iloc[i] if "vol_x" in df else float("nan")
            rows.append({"date": day, "time": f"{t:%H:%M}", "kind": kind, "side": v, "entry": round(entry, 1),
                         "net": round(net, 1), "best": round(best, 1), "worst": round(worst, 1), "result_at": how,
                         "vwap_ok": None if pd.isna(vg) else bool(sign * vg > 0),
                         "vol_1_5x": None if pd.isna(vx) else bool(vx >= 1.5),
                         "with_open": bool(sign * (df["close"].iloc[i] - df["open"].iloc[0]) > 0)})
    return rows


def stat(g):
    n = len(g)
    if n == 0:
        return "0 alerts"
    w = (g["net"] > 0).sum()
    return f"{n} alerts, {w / n:.0%} won, avg {g['net'].mean():+.0f}, total {g['net'].sum():+.0f} pts"


def minutes(s):
    return int(s[:2]) * 60 + int(s[3:5])


def build_report(month, alerts, days):
    a = alerts
    out = [f"MONTH REPORT {month}", f"{len(days)} trading days with data, {len(a)} alerts (index points, in the alert's direction)", ""]
    if len(days) < MIN_DAYS_TRUST:
        out += [f"CAUTION: only {len(days)} days. Under {MIN_DAYS_TRUST} days, win rates are too noisy to trade on.", ""]
    out.append("BY SIGNAL TYPE (opening break: held to 15:15 with stop at the range middle; others: 60 min later)")
    for _, kind in KINDS:
        out.append(f"{kind}: " + stat(a[a["kind"] == kind]))
    out += ["", "BY TIME OF DAY (all types except opening break)"]
    rest = a[a["kind"] != "OPENING BREAK"]
    for name, lo, hi in BUCKETS:
        m = rest["time"].map(minutes)
        out.append(f"{name}: " + stat(rest[(m >= lo[0] * 60 + lo[1]) & (m < hi[0] * 60 + hi[1])]))
    out += ["", "FILTER CHECK (all types except opening break; does the filter help?)"]
    for col, name in (("vwap_ok", "price on the right side of VWAP"), ("vol_1_5x", "volume 1.5x average or more"),
                      ("with_open", "in line with price vs the day's open")):
        k = rest[rest[col].notna()]
        if len(k):
            out.append(f"{name}: yes {stat(k[k[col] == True])} | no {stat(k[k[col] == False])}")
    best, worst = a.sort_values("net").tail(3), a.sort_values("net").head(3)
    out += ["", "BEST 3: " + ", ".join(f"{r.date[5:]} {r.time} {r.kind.title()} {r.net:+.0f}" for r in best.itertuples()),
            "WORST 3: " + ", ".join(f"{r.date[5:]} {r.time} {r.kind.title()} {r.net:+.0f}" for r in worst.itertuples()),
            "", "Alerts here come from CSV flags; days saved before the futures fix have no volume or VWAP, so the live scanner may differ.",
            "Not financial advice."]
    return "\n".join(out)


def main():
    now = datetime.now(IST)
    month = (os.environ.get("MONTH") or now.strftime("%Y-%m")).strip()
    force = os.environ.get("FORCE", "").lower() == "yes" or bool(os.environ.get("MONTH"))
    if not force and (now + timedelta(days=1)).month == now.month:
        print("Not the last day of the month; nothing to do.")
        return
    files = sorted(glob.glob(f"data/nifty_{month}-*.csv"))
    if not files:
        tg_text(f"Month report {month}: no day files found in data/. Run 'Day data file' for each day first.")
        return
    frames, rows, days = [], [], []
    for f in files:
        day = os.path.basename(f)[6:16]
        df = load_day(f)
        days.append(day)
        d = df.copy()
        d.insert(0, "date", day)
        frames.append(d)
        rows += day_alerts(df, day)
    pd.concat(frames).to_csv(f"month_{month}.csv")
    a = pd.DataFrame(rows)
    if a.empty:
        tg_text(f"Month report {month}: {len(days)} days found but no alerts in them.")
        return
    a.to_csv(f"alerts_{month}.csv", index=False)
    text = build_report(month, a, days)
    for i in range(0, len(text), 3900):
        tg_text(text[i:i + 3900])
    tg_file(f"alerts_{month}.csv", f"All alerts {month} with results")
    tg_file(f"month_{month}.csv", f"All minute data {month} ({len(days)} days)")


if __name__ == "__main__":
    main()
