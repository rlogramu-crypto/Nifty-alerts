"""
paper_trader.py - PAPER (simulated) option trades for every live alert. Places NO orders, needs no static IP.

How it works
  1. When the scanner sends a CALL / PUT alert (any live kind, SWING included), queue_signal() notes it. Right AFTER the
     alert has been sent, open_pending() reads the option chain and picks the option on the alert's side (CE for CALL,
     PE for PUT) whose price is between PREMIUM_MIN and PREMIUM_MAX (20 to 25), the one closest to the middle (22.5).
     One row per alert is saved to alerts/paper_YYYY-MM-DD.csv. If no strike is priced in that range the row says
     NO STRIKE and shows the nearest price, so you can see it was skipped and why.
  2. After the close, resolve_day() downloads that option's 1-minute candles and replays the trade two ways:
       fixed  : stop-loss 3 below the entry, target 10 above the entry, nothing moves.
       ladder : starts the same (stop -3, target +10). The first time the price is 2 points above the entry, the
                stop-loss moves to 1 point ABOVE the entry (the target stays). Then for every further 2 points the
                price gains (+4, +6, ...) the stop and the target both move up 2 points:
                  gained +2 -> stop entry+1, target entry+10
                  gained +4 -> stop entry+3, target entry+12
                  gained +6 -> stop entry+5, target entry+14   ... and so on.
       wide6  : stop 6 below the entry, target 15 above, nothing moves (a stop wider than normal 1-minute noise).
       wide8  : stop 8 below the entry, target 20 above, nothing moves.
     All four are logged side by side so you can compare them on the same trades. Change the numbers below to test others.
  2b. A second paper trade is opened for the SAME alert on the ATM option (the strike nearest the index). Its premium is
     much higher, so its stops and targets are the same four rules multiplied by (its delta / 0.3): an ATM option with
     delta 0.5 uses 5/17, 10/25 ... instead of 3/10, 6/15 ... (an option moves about "delta" points per index point, so
     this keeps the stop at the same distance in INDEX points, roughly 20 to 27 index points for wide6 / wide8).
     Switch the ATM trade off with PAPER_ATM=off.
  3. tally() gives a summary over all days (win rate, average and total points, rupees at your lot size) and a
     breakdown by alert type (so SWING's option results can be judged on their own).

Premium trace: for every paper trade resolve_day() also saves the option's 1-minute prices (open/high/low/close/volume,
from 10 minutes before the alert to the end of the day) to alerts/premium_trace_YYYY-MM-DD.csv, one block per trade
and mode (OTM / ATM). With it you can re-test ANY other stop, target or trail rule offline on real option prices,
without waiting for new trading days.

Simulation rules (kept conservative on purpose)
  * Entry = the OPEN of the first 1-minute candle after the alert was sent (about the price you could get within a
    minute). The chain price is only used to choose the strike; it is saved as chain_ltp.
  * Inside one candle the order of high and low is unknown. If a candle touches both the stop and the target, the stop
    is counted first. A candle that opens beyond a level exits at its open (a gap is not filled at the level).
  * The ladder levels move only after a candle closes, based on its high (no gain from moves inside the candle).
  * Any trade still open at 15:15 exits at the open of the first candle at or after 15:15.
  * Each side loses SLIPPAGE_PTS (a guess at the bid-ask gap), so net = gross - 2 x SLIPPAGE_PTS. Brokerage, STT and
    other charges are NOT included. Rupees = points x LOT_SIZE.
Everything is in option-premium points, not index points.
"""
import glob
import math
import os
from datetime import datetime, timedelta, timezone

import pandas as pd

PAPER_DIR = os.environ.get("ALERT_LOG_DIR", "alerts")      # one CSV per day, next to the alert logs: alerts/paper_2026-11-23.csv
PREMIUM_MIN, PREMIUM_MAX = 20.0, 25.0                      # entry price window
PREMIUM_AIM = 22.5                                         # pick the strike priced closest to this
SL_PTS, TARGET_PTS = 3.0, 10.0                             # stop-loss 3 below the entry, target 10 above it
FIRST_TRIGGER_PTS = 2.0                                    # ladder: when the price is this far above the entry ...
FIRST_STOP_ABOVE = 1.0                                     # ... the stop-loss moves to this far ABOVE the entry
STEP_PTS = 2.0                                             # then every further STEP_PTS gained, stop and target both move STEP_PTS
SLIPPAGE_PTS = 0.5                                         # per side, a guess
EXIT_AT = (15, 15)                                         # still open then: exit
ENTRY_LAST = (15, 10)                                      # an alert after this time is not paper-traded (status TOO LATE)
LOT_SIZE = 65                                              # Nifty lot size since the January 2026 contracts (only a multiplier)
IST = timezone(timedelta(hours=5, minutes=30))
# Charges per round trip (buy order + sell order) on Upstox, options. Rates change; check Upstox's charge calculator.
BROKERAGE_PER_ORDER = 20.0        # Rs, flat per executed order
STT_ON_SELL = 0.0015              # 0.15% of the premium sold (older Upstox pages still show 0.1%)
EXCHANGE_TXN = 0.000495           # NSE options, % of premium, both sides (Upstox's own page; other lists show about 0.0355%)
SEBI_FEE = 10 / 1e7               # Rs 10 per crore
STAMP_ON_BUY = 0.00003            # 0.003% of the premium bought
GST = 0.18                        # on brokerage + exchange charge + SEBI fee
VARIANTS = ("fixed", "ladder", "wide6", "wide8")
RULES = {                                                  # option points for an option priced about 20-25
    "fixed": dict(stop=3.0, target=10.0),
    "ladder": dict(stop=3.0, target=10.0, ladder=True),
    "wide6": dict(stop=6.0, target=15.0),
    "wide8": dict(stop=8.0, target=20.0),
}
LABELS = {"fixed": "fixed 3/10", "ladder": "ladder (+2 -> stop +1, then +2 per +2)", "wide6": "wide 6/15", "wide8": "wide 8/20"}
ATM_TOO = os.environ.get("PAPER_ATM", "on").lower() not in ("off", "0", "no")
REF_DELTA = 0.3                                            # delta of an option priced about 20-25; ATM rules are scaled by delta / this
DEFAULT_ATM_DELTA = 0.5                                    # used when the chain gives no delta
SCALE_MIN, SCALE_MAX = 0.5, 3.5

COLS = (["date", "candle_time", "opened_at", "kinds", "signal", "opt_type", "mode", "delta", "scale", "expiry", "strike", "instrument_key",
         "chain_ltp", "bid", "ask", "status", "note", "entry_time", "entry", "mfe60", "mae60"]
        + [f"{v}_{k}" for v in VARIANTS for k in ("reason", "exit", "net", "min", "exit_time", "start_stop", "start_target",
                                                  "pnl_rs", "charges_rs", "real_pnl_rs", "result")]
        + ["ladder_moves"])

_QUEUE = []


def enabled():
    return os.environ.get("PAPER_TRADING", "on").lower() not in ("off", "0", "no")


def day_path(day, paper_dir=None):
    return os.path.join(paper_dir or PAPER_DIR, f"paper_{day}.csv")


def _read(path):
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for c in COLS:
        if c not in df.columns:
            df[c] = ""
    return df[COLS]


TRACE_BEFORE_MIN = 10                                      # minutes of premium history saved before the alert
TRACE_COLS = ["date", "alert_time", "mode", "signal", "opt_type", "strike", "time", "open", "high", "low", "close", "volume",
              "after_entry"]


def trace_path(day, paper_dir=None):
    return os.path.join(paper_dir or PAPER_DIR, f"premium_trace_{day}.csv")     # not named paper_*: tally() reads paper_*.csv


def _save_trace(day, r, cdl, opened):
    """Append one paper trade's 1-minute option prices to the day's trace file. Never raises, never writes a trade twice."""
    try:
        if not len(cdl):
            return
        w = cdl[cdl.index >= opened.floor("min") - pd.Timedelta(minutes=TRACE_BEFORE_MIN)]
        if not len(w):
            return
        mode = r["mode"] or "OTM"
        out = pd.DataFrame({
            "date": day, "alert_time": r["candle_time"], "mode": mode, "signal": r["signal"], "opt_type": r["opt_type"],
            "strike": r["strike"], "time": [f"{t:%H:%M}" for t in w.index],
            "open": w["Open"].round(2).values, "high": w["High"].round(2).values, "low": w["Low"].round(2).values,
            "close": w["Close"].round(2).values,
            "volume": w["Volume"].values if "Volume" in w.columns else "",
            "after_entry": (w.index > opened).astype(int)}, columns=TRACE_COLS)
        path = trace_path(day)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            old = pd.read_csv(path, dtype=str, keep_default_na=False)
            if ((old["alert_time"] == r["candle_time"]) & (old["mode"] == mode) & (old["signal"] == r["signal"])).any():
                return                                        # already saved (a retry)
        out.to_csv(path, mode="a", header=not os.path.exists(path), index=False)
    except Exception as e:                                    # noqa: BLE001 - never let the trace break the paper results
        print(f"  premium trace not saved: {e}")


def _append(path, row):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cur = _read(path)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            if fh.readline().strip().split(",") != COLS:        # older file with fewer columns: rewrite first
                cur.to_csv(path, index=False)
    full = {c: "" for c in COLS}
    full.update(row)
    pd.DataFrame([full], columns=COLS).to_csv(path, mode="a", header=not os.path.exists(path), index=False)


# ---------------------------------------------------------------- 1. choosing the option
def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def pick_option(rows, d, lo=None, hi=None, aim=None):
    """rows: Upstox option-chain list. d = +1 (CALL -> CE) or -1 (PUT -> PE).
    Returns (choice or None, note). choice = dict(strike, key, ltp, bid, ask)."""
    lo = PREMIUM_MIN if lo is None else lo
    hi = PREMIUM_MAX if hi is None else hi
    aim = PREMIUM_AIM if aim is None else aim
    side = "call_options" if d > 0 else "put_options"
    cands, priced = [], []
    for x in rows or []:
        opt = x.get(side) or {}
        md = opt.get("market_data") or {}
        ltp, key, strike = _num(md.get("ltp")), opt.get("instrument_key"), _num(x.get("strike_price"))
        if ltp is None or ltp <= 0 or not key or strike is None:
            continue
        priced.append((ltp, strike))
        if lo <= ltp <= hi:
            cands.append(dict(strike=strike, key=key, ltp=ltp, bid=_num(md.get("bid_price")), ask=_num(md.get("ask_price")),
                              delta=_num((opt.get("option_greeks") or {}).get("delta"))))
    if cands:
        best = min(cands, key=lambda c: (abs(c["ltp"] - aim), c["strike"]))
        return best, f"{len(cands)} strikes in range"
    if not priced:
        return None, "chain had no option prices"
    near = min(priced, key=lambda p: min(abs(p[0] - lo), abs(p[0] - hi)))
    return None, f"no price between {lo:g} and {hi:g}; nearest {near[0]:g} at strike {near[1]:g}"


def pick_atm(rows, d):
    """The option on the alert's side at the strike nearest the index (spot taken from the chain). Returns (choice or None, note)."""
    side = "call_options" if d > 0 else "put_options"
    spot = next((v for v in (_num(x.get("underlying_spot_price")) for x in rows or []) if v), None)
    if spot is None:
        return None, "chain has no spot price"
    best = None
    for x in rows:
        opt = x.get(side) or {}
        md = opt.get("market_data") or {}
        ltp, key, strike = _num(md.get("ltp")), opt.get("instrument_key"), _num(x.get("strike_price"))
        if ltp is None or ltp <= 0 or not key or strike is None:
            continue
        if best is None or abs(strike - spot) < best[0]:
            best = (abs(strike - spot), dict(strike=strike, key=key, ltp=ltp, bid=_num(md.get("bid_price")), ask=_num(md.get("ask_price")),
                                             delta=_num((opt.get("option_greeks") or {}).get("delta"))))
    if best is None:
        return None, "no priced ATM option"
    return best[1], f"ATM (index {spot:g})"


def atm_scale(delta):
    d = abs(delta) if delta else DEFAULT_ATM_DELTA
    return max(SCALE_MIN, min(SCALE_MAX, d / REF_DELTA))


def queue_signal(candle_time, d, kinds, sent_at):
    """Called by the scanner when an alert is created. Cheap and never raises."""
    if enabled():
        _QUEUE.append((candle_time, d, kinds, sent_at))


def open_pending(fetch_rows, now=None):
    """Open the queued paper trades (one on the 20-25 option and, unless PAPER_ATM=off, one on the ATM option per alert).
    fetch_rows() -> (expiry, chain_rows). Call it AFTER the alerts were sent, because it makes a network call.
    Returns a list of short Telegram messages (one per alert)."""
    if not _QUEUE:
        return []
    items = list(_QUEUE)
    _QUEUE.clear()
    now = now or datetime.now(IST)
    late = (now.hour, now.minute) > ENTRY_LAST
    try:
        expiry, rows = (None, []) if late else fetch_rows()
        err = None
    except Exception as e:                                       # noqa: BLE001 - the scanner must never stop for this
        expiry, rows, err = None, [], f"option chain failed: {e}"
    msgs = []
    for candle_time, d, kinds, sent_at in items:
        made = []
        for mode in (("OTM", "ATM") if ATM_TOO else ("OTM",)):
            base = dict(date=f"{candle_time:%Y-%m-%d}", candle_time=f"{candle_time:%H:%M}", opened_at=f"{now:%Y-%m-%d %H:%M:%S}",
                        kinds=kinds, signal="CALL" if d > 0 else "PUT", opt_type="CE" if d > 0 else "PE", mode=mode,
                        expiry=expiry or "", scale="1")
            if late:
                row = dict(base, status="TOO LATE", note=f"alert after {ENTRY_LAST[0]}:{ENTRY_LAST[1]:02d}")
            elif err:
                row = dict(base, status="NO CHAIN", note=err)
            else:
                choice, note = pick_option(rows, d) if mode == "OTM" else pick_atm(rows, d)
                if choice is None:
                    row = dict(base, status="NO STRIKE", note=note)
                else:
                    sc = 1.0 if mode == "OTM" else atm_scale(choice["delta"])
                    row = dict(base, strike=f"{choice['strike']:g}", instrument_key=choice["key"], chain_ltp=f"{choice['ltp']:g}",
                               bid="" if choice["bid"] is None else f"{choice['bid']:g}",
                               ask="" if choice["ask"] is None else f"{choice['ask']:g}", status="OPEN", note=note,
                               delta="" if choice["delta"] is None else f"{choice['delta']:.2f}", scale=f"{sc:.2f}")
            _append(day_path(base["date"]), row)
            made.append(row)
        head = f"{made[0]['signal']} alert {made[0]['candle_time']} ({kinds})"
        if made[0]["status"] != "OPEN" and all(r["status"] != "OPEN" for r in made):
            msgs.append(f"PAPER: {head} not traded ({made[0]['status']}: {made[0]['note']}). No real order.")
            continue
        lines = [f"PAPER TRADE (no real order): {head}"]
        for r in made:
            if r["status"] == "OPEN":
                x = float(r["scale"])
                rules = "3/10, ladder, 6/15, 8/20" if x == 1.0 else \
                    f"{3 * x:.0f}/{10 * x:.0f}, ladder, {6 * x:.0f}/{15 * x:.0f}, {8 * x:.0f}/{20 * x:.0f} (x{x:.1f}, delta {r['delta'] or '?'})"
                lines.append(f"{r['mode']}: BUY {r['strike']} {r['opt_type']} ({r['expiry']}), chain price {r['chain_ltp']} "
                             f"(bid {r['bid'] or '-'} / ask {r['ask'] or '-'}). Stop/target: {rules}")
            else:
                lines.append(f"{r['mode']}: not traded ({r['status']}: {r['note']})")
        lines.append("Fill = next minute's open. Results come after the close.")
        msgs.append("\n".join(lines))
    return msgs


# ---------------------------------------------------------------- 2. replaying the trade
def charges_rs(entry, exit_px):
    """Brokerage + STT + exchange + SEBI + stamp duty + GST (Rs) for buying at entry and selling at exit_px, one lot.
    The slippage (half a point each side) is treated as part of the fill prices, as in net = exit - entry - 2 x slippage."""
    buy = (entry + SLIPPAGE_PTS) * LOT_SIZE
    sell = max(exit_px - SLIPPAGE_PTS, 0.0) * LOT_SIZE
    brok = 2 * BROKERAGE_PER_ORDER
    txn = EXCHANGE_TXN * (buy + sell)
    sebi = SEBI_FEE * (buy + sell)
    return brok + STT_ON_SELL * sell + txn + sebi + STAMP_ON_BUY * buy + GST * (brok + txn + sebi)


def ladder_levels(entry, best, sl_pts=None, target_pts=None, first_trigger=None, first_above=None, step=None):
    """Stop and target for the ladder variant when the best price so far is `best`."""
    sl_pts = SL_PTS if sl_pts is None else sl_pts
    target_pts = TARGET_PTS if target_pts is None else target_pts
    first_trigger = FIRST_TRIGGER_PTS if first_trigger is None else first_trigger
    first_above = FIRST_STOP_ABOVE if first_above is None else first_above
    step = STEP_PTS if step is None else step
    gain = best - entry
    if gain + 1e-9 < first_trigger:
        return entry - sl_pts, entry + target_pts
    k = math.floor((gain - first_trigger) / step + 1e-9)          # ladder steps beyond the first (0 at +2, 1 at +4, ...)
    return entry + first_above + k * step, entry + target_pts + k * step


def simulate(c, variant, scale=1.0, exit_at=None):
    """c: 1-minute candles (Open/High/Low/Close, index = time) starting with the entry candle.
    variant: one of VARIANTS. scale multiplies every distance (1 for the 20-25 option, delta/0.3 for the ATM option).
    Returns dict(reason, exit, minutes). Prices are the exit level before slippage."""
    rule = RULES[variant]
    sl_pts, target_pts, lad = rule["stop"] * scale, rule["target"] * scale, bool(rule.get("ladder"))
    exit_at = exit_at or EXIT_AT
    entry = float(c["Open"].iloc[0])
    t0 = c.index[0]
    sl, tp, best = entry - sl_pts, entry + target_pts, entry
    moves = []                                                   # ladder only: (minute, new stop, new target)

    def done(reason, price, t):
        return dict(reason=reason, exit=price, minutes=int(round((t - t0).total_seconds() / 60)),
                    time=f"{t:%H:%M}", moves=moves, stop0=entry - sl_pts, target0=entry + target_pts)

    for t, r in c.iterrows():
        o, h, lo_, cl = float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"])
        if (t.hour, t.minute) >= exit_at:
            return done("EOD 15:15", o, t)
        moved = sl > entry - sl_pts + 1e-9
        if o <= sl:
            return done("LADDER SL (gap)" if moved else "SL (gap)", o, t)
        if o >= tp:
            return done("TARGET (gap)", o, t)
        if lo_ <= sl:                                            # stop first when one candle touches both
            return done("LADDER SL" if moved else "SL", sl, t)
        if h >= tp:
            return done("TARGET", tp, t)
        if lad:
            best = max(best, h)
            nsl, ntp = ladder_levels(entry, best, sl_pts, target_pts, FIRST_TRIGGER_PTS * scale, FIRST_STOP_ABOVE * scale, STEP_PTS * scale)
            if abs(nsl - sl) > 1e-9:
                moves.append((f"{t:%H:%M}", nsl, ntp))            # the minute whose high caused it; applies from the next minute
            sl, tp = nsl, ntp
    return done("EOD (data ended)", float(c["Close"].iloc[-1]), c.index[-1])


def _result(net):
    """WIN / LOSS / FLAT from the net points (a loss is a NEGATIVE number in the *_net and *_pnl_rs columns)."""
    return "WIN" if net > 0 else "LOSS" if net < 0 else "FLAT"


def _apply_variants(log, idx, after, scale):
    """Replay the four rules on the candles `after` (first candle = entry) and write every result column of row idx."""
    entry = float(after["Open"].iloc[0])
    moves_text = ""
    for v in VARIANTS:
        res = simulate(after, v, scale)
        net = res["exit"] - entry - 2 * SLIPPAGE_PTS
        pnl = net * LOT_SIZE
        chg = charges_rs(entry, res["exit"])
        log.loc[idx, [f"{v}_{k}" for k in ("reason", "exit", "net", "min", "exit_time", "start_stop", "start_target",
                                          "pnl_rs", "charges_rs", "real_pnl_rs", "result")]] = [
            res["reason"], f"{res['exit']:.2f}", f"{net:.2f}", str(res["minutes"]), res["time"],
            f"{res['stop0']:.2f}", f"{res['target0']:.2f}", f"{pnl:.0f}", f"{chg:.0f}", f"{pnl - chg:.0f}", _result(net)]
        if v == "ladder":
            moves_text = "; ".join(f"{m} stop {a:.2f} target {b:.2f}" for m, a, b in res["moves"]) or "none"
    log.loc[idx, "ladder_moves"] = moves_text


def resolve_day(fetch_candles, day, path=None):
    """Fill the results of the day's OPEN paper trades. fetch_candles(instrument_key) -> DataFrame (Open/High/Low/Close,
    time index). Returns the number of trades resolved."""
    path = path or day_path(day)
    log = _read(path)
    if log.empty:
        return 0
    cache, n_done = {}, 0
    for idx in log.index[log["status"].isin(["OPEN", "NO DATA"]) & (log["fixed_reason"] == "")]:
        r = log.loc[idx]
        key = r["instrument_key"]
        try:
            if key not in cache:
                cache[key] = fetch_candles(key)
            cdl = cache[key]
        except (Exception, SystemExit) as e:                     # noqa: BLE001
            log.loc[idx, ["status", "note"]] = ["NO DATA", f"candles failed: {e}"]
            continue
        opened = pd.Timestamp(r["opened_at"])
        if len(cdl) and cdl.index.tz is not None and opened.tzinfo is None:
            opened = opened.tz_localize(cdl.index.tz)
        after = cdl[cdl.index > opened] if len(cdl) else cdl
        if not len(after):
            log.loc[idx, ["status", "note"]] = ["NO DATA", "no option candle after the alert"]
            continue
        _save_trace(day, r, cdl, opened)
        entry = float(after["Open"].iloc[0])
        log.loc[idx, ["entry_time", "entry"]] = [f"{after.index[0]:%H:%M}", f"{entry:.2f}"]
        w = after.iloc[:60]
        log.loc[idx, ["mfe60", "mae60"]] = [f"{max(0.0, w['High'].max() - entry):.2f}", f"{max(0.0, entry - w['Low'].min()):.2f}"]
        _apply_variants(log, idx, after, _num(r["scale"]) or 1.0)
        log.loc[idx, ["status", "note"]] = ["DONE", log.loc[idx, "note"] if log.loc[idx, "status"] == "OPEN" else "resolved on retry"]
        n_done += 1
    log.to_csv(path, index=False)
    return n_done


def rebuild_from_trace(paper_path, trace_path_):
    """Re-fill every result column of an existing day file from its premium trace (no network). Used to give a day that was
    saved with fewer columns the full per-trade profit and loss. Returns (rows rebuilt, rows whose net changed by > 0.01)."""
    log = _read(paper_path)
    tr = pd.read_csv(trace_path_, dtype=str, keep_default_na=False)
    for c in ("open", "high", "low", "close"):
        tr[c] = tr[c].astype(float)
    rebuilt = changed = 0
    for idx in log.index[log["status"] == "DONE"]:
        r = log.loc[idx]
        blk = tr[(tr["alert_time"] == r["candle_time"]) & (tr["mode"] == (r["mode"] or "OTM")) & (tr["signal"] == r["signal"])]
        if blk.empty:
            continue
        cdl = pd.DataFrame({"Open": blk["open"].values, "High": blk["high"].values, "Low": blk["low"].values, "Close": blk["close"].values},
                           index=pd.to_datetime(blk["date"] + " " + blk["time"]).values)
        after = cdl[cdl.index > pd.Timestamp(r["opened_at"]).to_datetime64()]
        if not len(after):
            continue
        old = {v: _num(r[f"{v}_net"]) for v in VARIANTS}
        _apply_variants(log, idx, after, _num(r["scale"]) or 1.0)
        rebuilt += 1
        if any(old[v] is not None and abs(old[v] - float(log.loc[idx, f"{v}_net"])) > 0.011 for v in VARIANTS):
            changed += 1
    log.to_csv(paper_path, index=False)
    return rebuilt, changed


# ---------------------------------------------------------------- 3. summary
def tally(paper_dir=None):
    """Text summary over all paper files (empty string if there is nothing yet)."""
    files = sorted(glob.glob(os.path.join(paper_dir or PAPER_DIR, "paper_*.csv")))
    frames = [_read(p) for p in files]
    log = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS)
    if log.empty:
        return ""
    log["mode"] = log["mode"].replace("", "OTM")
    done = log[log["fixed_reason"] != ""]
    skipped = log[~log["status"].isin(["OPEN", "DONE"])]
    out = [f"PAPER TRADES ({len(files)} days): {len(done)} traded, {len(skipped)} skipped of {len(log)} trades "
           f"(each alert = one {PREMIUM_MIN:g}-{PREMIUM_MAX:g} option trade + one ATM trade). Option points after "
           f"{2 * SLIPPAGE_PTS:g} slippage; Rs at lot {LOT_SIZE} also shown after brokerage, taxes and charges (Upstox rates)."]
    if len(skipped):
        out.append("  skipped: " + ", ".join(f"{k} {v}" for k, v in skipped["status"].value_counts().items()))
    if done.empty:
        return "\n".join(out)

    def real(rows, v):
        """Real P&L in Rs per trade: (exit - entry - slippage) x lot - charges."""
        e = pd.to_numeric(rows["entry"], errors="coerce")
        x = pd.to_numeric(rows[f"{v}_exit"], errors="coerce")
        return pd.Series([(xx - ee - 2 * SLIPPAGE_PTS) * LOT_SIZE - charges_rs(ee, xx) if pd.notna(ee) and pd.notna(xx) else float("nan")
                          for ee, xx in zip(e, x)], index=rows.index)

    def line(rows, v):
        net = pd.to_numeric(rows[f"{v}_net"], errors="coerce").dropna()
        if net.empty:
            return None
        wins = int((net > 0).sum())
        rp = real(rows, v).dropna()
        return (f"    {LABELS[v]}: {wins}/{len(net)} won = {wins / len(net):.0%}, avg {net.mean():+.1f} pts | "
                f"Rs {net.sum() * LOT_SIZE:+,.0f} after slippage, Rs {rp.sum():+,.0f} after charges "
                f"(profit {rp[rp > 0].sum():+,.0f}, loss {rp[rp <= 0].sum():+,.0f})")

    for mode, title in (("OTM", f"Option priced {PREMIUM_MIN:g}-{PREMIUM_MAX:g} (stops in points)"),
                        ("ATM", f"ATM option (stops = the same rules x delta/{REF_DELTA:g})")):
        sub = done[done["mode"] == mode]
        if sub.empty:
            continue
        out.append(f"  {title}, {len(sub)} trades:")
        out += [ln for ln in (line(sub, v) for v in VARIANTS) if ln]
    otm = done[done["mode"] == "OTM"]
    kinds = sorted({k for ks in otm["kinds"] for k in ks.split(" + ")})
    if len(kinds) > 1:                                    # by alert type (a merged alert counts under each of its types)
        out.append(f"  by alert type, {PREMIUM_MIN:g}-{PREMIUM_MAX:g} option, total pts ({' | '.join(VARIANTS)}):")
        for k in kinds:
            sub = otm[otm["kinds"].apply(lambda s, k=k: k in s.split(" + "))]
            tot = [pd.to_numeric(sub[f"{v}_net"], errors="coerce").sum() for v in VARIANTS]
            out.append(f"    {k}: {len(sub)} trades | " + " | ".join(f"{t:+.0f}" for t in tot))
    # expiry day (the options expire the same day: tiny premiums, huge swings) behaves very differently from other days
    done = done.assign(daytype=["expiry day" if e and e == d_ else "other days" for e, d_ in zip(done["expiry"], done["date"])])
    if done["daytype"].nunique() > 1 or (done["daytype"] == "expiry day").any():
        out.append("  by day type (both options together, total Rs after charges; expiry day = options expire that day):")
        for dt in ("expiry day", "other days"):
            sub = done[done["daytype"] == dt]
            if sub.empty:
                continue
            tot = [real(sub, v).sum() for v in VARIANTS]
            out.append(f"    {dt}: {sub['date'].nunique()} day(s), {len(sub)} trades | " + " | ".join(f"{t:+,.0f}" for t in tot)
                       + f"   ({' | '.join(VARIANTS)})")
    if len(otm) < 30:
        out.append("  too few trades to judge yet")
    return "\n".join(out)


# ---------------------------------------------------------------- 4. export with negative points and losses
def export_all(paper_dir=None):
    """Write two CSVs next to the day files (no network needed):
      all_paper_trades.csv : every trade of every day. For each rule: net points (NEGATIVE on a loss), pnl_rs (points x lot,
                             negative on a loss), charges_rs, real_pnl_rs (after charges) and result (WIN / LOSS / FLAT).
                             Old day files that lack these columns are filled in from their entry / exit / net values.
      paper_summary.csv    : per rule (and per OTM / ATM mode): wins, losses, winning points, LOSING points (negative),
                             net points, Rs after slippage, charges, Rs after charges, gross profit and gross loss in Rs.
    Returns (trades path, summary path), or None if there are no paper files."""
    d = paper_dir or PAPER_DIR
    files = sorted(glob.glob(os.path.join(d, "paper_2*.csv")))
    if not files:
        return None
    log = pd.concat([_read(p) for p in files], ignore_index=True)
    log["mode"] = log["mode"].replace("", "OTM")
    en = pd.to_numeric(log["entry"], errors="coerce")
    for v in VARIANTS:
        net = pd.to_numeric(log[f"{v}_net"], errors="coerce")
        ex = pd.to_numeric(log[f"{v}_exit"], errors="coerce")
        chg = pd.Series([charges_rs(e, x) if pd.notna(e) and pd.notna(x) else float("nan") for e, x in zip(en, ex)], index=log.index)
        log[f"{v}_pnl_rs"] = (net * LOT_SIZE).round(0)
        log[f"{v}_charges_rs"] = chg.round(0)
        log[f"{v}_real_pnl_rs"] = (net * LOT_SIZE - chg).round(0)
        log[f"{v}_result"] = [_result(n) if pd.notna(n) else "" for n in net]
    tpath, spath = os.path.join(d, "all_paper_trades.csv"), os.path.join(d, "paper_summary.csv")
    log.to_csv(tpath, index=False)
    rows = []
    for mode in ("ALL", "OTM", "ATM"):
        sub = log if mode == "ALL" else log[log["mode"] == mode]
        for v in VARIANTS:
            net = pd.to_numeric(sub[f"{v}_net"], errors="coerce").dropna()
            if net.empty:
                continue
            real = pd.to_numeric(sub[f"{v}_real_pnl_rs"], errors="coerce").dropna()
            rows.append({"mode": mode, "rule": v, "trades": len(net), "wins": int((net > 0).sum()), "losses": int((net < 0).sum()),
                         "win_pct": round(100 * (net > 0).mean(), 1), "winning_points": round(net[net > 0].sum(), 2),
                         "losing_points": round(net[net < 0].sum(), 2), "net_points": round(net.sum(), 2),
                         "pnl_rs": round(net.sum() * LOT_SIZE), "charges_rs": round(pd.to_numeric(sub[f"{v}_charges_rs"], errors="coerce").sum()),
                         "gross_profit_rs": round(real[real > 0].sum()), "gross_loss_rs": round(real[real < 0].sum()),
                         "real_pnl_rs": round(real.sum())})
    pd.DataFrame(rows).to_csv(spath, index=False)
    return tpath, spath


if __name__ == "__main__":      # python paper_trader.py  -> print the summary of the paper files
    print(tally() or "No paper trades yet.")
    paths = export_all()                                  # also writes all_paper_trades.csv and paper_summary.csv
    if paths:
        print("\nwrote " + " and ".join(paths))
