"""
Pre-market brief from Upstox -> Telegram (and GitHub run summary). Needs only: requests.

Env vars:
  UPSTOX_TOKEN        Upstox Analytics Token (read-only)
  TELEGRAM_BOT_TOKEN  Telegram bot token   (optional; skips Telegram if missing)
  TELEGRAM_CHAT_ID    Telegram chat id     (optional)

Not available from Upstox, so NOT included: market breadth %, event calendar.
Information only, not investment advice.
"""
import os, sys, gzip, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import requests

BASE = "https://api.upstox.com"
IST = ZoneInfo("Asia/Kolkata")
NIFTY, BANK, VIX = "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank", "NSE_INDEX|India VIX"
GLOBAL_URL = "https://assets.upstox.com/market-quote/instruments/exchange/global.json.gz"
GIFT_FALLBACK = "GLOBAL_INDEX|SGX NIFTY"
GLOBAL_WANT = [("Dow", "DOW JONES"), ("S&P", "S&P"), ("Nasdaq", "US TECH 100"),
               ("Nikkei", "NIKKEI 225"), ("HangSeng", "HANG SENG")]

token = os.environ.get("UPSTOX_TOKEN")
if not token:
    sys.exit("Set UPSTOX_TOKEN first.")
HEAD = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
NOW = datetime.now(IST)

def get(path, params=None):
    r = requests.get(BASE + path, headers=HEAD, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {path}: {r.text[:200]}")
    return r.json().get("data")

def safe(fn, label):
    try:
        return fn()
    except Exception as e:
        print(f"WARNING: {label} skipped -> {e}")
        return None

def candles(key, days=20, include_today=True):
    to_d = NOW.strftime("%Y-%m-%d")
    from_d = (NOW - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = get(f"/v3/historical-candle/{quote(key, safe='')}/days/1/{to_d}/{from_d}")["candles"]
    done = NOW.hour * 60 + NOW.minute >= 15 * 60 + 45
    out = [c for c in rows if include_today or c[0][:10] != to_d or done]
    out.sort(key=lambda c: c[0], reverse=True)
    return out

def pct(c):  # latest vs previous close
    return c[0][4] / c[1][4] - 1

def sig(v, up, down):
    if v is None: return None
    return "Bullish" if v >= up else "Bearish" if v <= -down else "Neutral"

def f(v, nd=2): return "n/a" if v is None else f"{v:,.{nd}f}"
def p(v): return "n/a" if v is None else f"{v*100:+.2f}%"
def tag(s): return "" if s is None else {"Bullish": " 🟢", "Bearish": " 🔴", "Neutral": " ⚪"}[s]

signals = {}

# ---- Nifty and Bank Nifty (completed sessions only)
nf = safe(lambda: candles(NIFTY, include_today=False), "Nifty")
bk = safe(lambda: candles(BANK, include_today=False), "Bank Nifty")
close = hi = lo = nchg = None
if nf and len(nf) >= 2:
    close, hi, lo, nchg = nf[0][4], nf[0][2], nf[0][3], pct(nf)
# Today's open (available once NSE opens at 9:15 IST)
open_gap = today_open = None
nt = safe(lambda: candles(NIFTY, include_today=True), "Nifty today")
if nt and close and nt[0][0][:10] == NOW.strftime("%Y-%m-%d") and NOW.hour * 60 + NOW.minute >= 9 * 60 + 15:
    today_open = nt[0][1]
    open_gap = today_open / close - 1
bchg = pct(bk) if bk and len(bk) >= 2 else None
if nchg is not None and bchg is not None:
    signals["Bank Nifty"] = ("Bullish" if nchg > 0 and bchg > 0 else
                             "Bearish" if nchg < 0 and bchg < 0 else "Neutral")

# ---- VIX
vx = safe(lambda: candles(VIX, include_today=False), "VIX")
vix = vchg = None
if vx and len(vx) >= 2:
    vix, vchg = vx[0][4], pct(vx)
    signals["VIX"] = sig(-vchg, 0.03, 0.03)  # falling VIX = Bullish

# ---- Global instruments (GIFT Nifty, US, Asia)
def load_global():
    r = requests.get(GLOBAL_URL, timeout=60); r.raise_for_status()
    try: raw = gzip.decompress(r.content)
    except Exception: raw = r.content
    return [i for i in json.loads(raw) if i.get("segment") == "GLOBAL_INDEX"]
gl = safe(load_global, "global instruments file") or []
def find(sub):
    sub = sub.upper()
    for i in gl:
        if i["name"].upper() == sub: return i["instrument_key"]
    for i in gl:
        if sub in i["name"].upper(): return i["instrument_key"]
    return None

gift_key = find("GIFT NIFTY") or GIFT_FALLBACK
gc = safe(lambda: candles(gift_key), "GIFT Nifty")
gift = gap = None
if gc and len(gc) >= 1:
    gift = gc[0][4]
    if close: gap = gift / close - 1

gap_src = open_gap if open_gap is not None else gap
if gap_src is not None:
    signals["Gap"] = sig(gap_src, 0.003, 0.003)

world = {}
for label, name in GLOBAL_WANT:
    k = find(name)
    if not k: continue
    c = safe(lambda: candles(k), label)
    if c and len(c) >= 2: world[label] = pct(c)
if world:
    score = sum(1 for v in world.values() if v > 0.003) - sum(1 for v in world.values() if v < -0.003)
    signals["US/Asia"] = "Bullish" if score >= 2 else "Bearish" if score <= -2 else "Neutral"

# ---- FII / DII cash flows
def flow(kind):
    d = get(f"/v2/market/{kind}", {"data_type": "NSE_EQ|CASH", "interval": "1D"})["NSE_EQ|CASH"]
    d.sort(key=lambda r: r["time_stamp"], reverse=True)
    r = d[0]
    cr = lambda x: x / 1e7 if abs(x) > 1e8 else x   # handles INR or crore units
    return cr(r["buy_amount"]) - cr(r["sell_amount"]), datetime.fromtimestamp(r["time_stamp"] / 1000, IST)
fii = safe(lambda: flow("fii"), "FII")
dii = safe(lambda: flow("dii"), "DII")
if fii: signals["FII flow"] = sig(fii[0], 500, 500)

# ---- Option chain: PCR and nearest OI walls
pcr = sup = res = sup_all = res_all = None
expiry = "n/a"
def chain_data():
    global pcr, sup, res, sup_all, res_all, expiry
    con = get("/v2/option/contract", {"instrument_key": NIFTY})
    today = NOW.strftime("%Y-%m-%d")
    expiry = sorted({c["expiry"][:10] for c in con if c["expiry"][:10] >= today})[0]
    ch = get("/v2/option/chain", {"instrument_key": NIFTY, "expiry_date": expiry})
    po, co = {}, {}
    for row in ch:
        k = row["strike_price"]
        co[k] = (row.get("call_options") or {}).get("market_data", {}).get("oi") or 0
        po[k] = (row.get("put_options") or {}).get("market_data", {}).get("oi") or 0
    tc, tp = sum(co.values()), sum(po.values())
    pcr = round(tp / tc, 2) if tc else None
    sup_all, res_all = max(po, key=po.get), max(co, key=co.get)
    ref = close or gift
    if ref:
        pw = {k: v for k, v in po.items() if ref - 600 <= k <= ref}
        cw = {k: v for k, v in co.items() if ref <= k <= ref + 600}
        sup = max(pw, key=pw.get) if pw else None
        res = max(cw, key=cw.get) if cw else None
safe(chain_data, "option chain")
if pcr is not None:
    signals["PCR"] = "Bullish" if pcr >= 1.2 else "Bearish" if pcr <= 0.8 else "Neutral"

# ---- Bias
bull = sum(1 for s in signals.values() if s == "Bullish")
bear = sum(1 for s in signals.values() if s == "Bearish")
n = len(signals)
bias = ("Not enough data" if n < 4 else
        "Favor CE setups" if bull >= 3 and bull > bear else
        "Favor PE setups" if bear >= 3 and bear > bull else
        "Mixed: trade less or skip")

L = [f"📊 PRE-MARKET {NOW:%a %d-%b-%Y %H:%M} IST",
     "",
     f"Nifty prev: {f(close)} ({p(nchg)})",
     f"Levels: support {f(lo)} | resistance {f(hi)}",
     f"CE trigger: 5-min close above {f(hi)} (or 15-min high if higher)",
     f"PE trigger: 5-min close below {f(lo)} (or 15-min low if lower)",
     "",
     (f"Today's open: {f(today_open)} ({p(open_gap)} gap){tag(signals.get('Gap'))}" if today_open
      else f"Today's open: not yet available (run after 9:15 IST)"),
     f"GIFT Nifty: {f(gift)} ({p(gap)} vs prev close)" + ("" if today_open else tag(signals.get("Gap"))),
     "Global: " + (", ".join(f"{k} {p(v)}" for k, v in world.items()) or "n/a") + tag(signals.get("US/Asia")),
     f"VIX: {f(vix)} ({p(vchg)}){tag(signals.get('VIX'))}",
     f"FII cash net: " + ("n/a" if not fii else f"₹{fii[0]:,.0f} cr ({fii[1]:%d-%b})") + tag(signals.get("FII flow")),
     f"DII cash net: " + ("n/a" if not dii else f"₹{dii[0]:,.0f} cr ({dii[1]:%d-%b})"),
     f"Bank Nifty prev: {p(bchg)}{tag(signals.get('Bank Nifty'))}",
     f"PCR ({expiry}): {pcr if pcr is not None else 'n/a'}{tag(signals.get('PCR'))}",
     f"OI walls near spot: put {sup or 'n/a'} | call {res or 'n/a'}",
     f"(Chain-wide max OI: put {sup_all or 'n/a'} | call {res_all or 'n/a'})",
     "",
     f"Signals: {bull} bullish, {bear} bearish, {n - bull - bear} neutral",
     f"BIAS: {bias}",
     "",
     "Check yourself: breadth, events (RBI/Fed/CPI/earnings).",
     "Plan: 1 lot, 5-pt stop, 10-pt target, max 4 trades, stop after 2 losses.",
     "Info only, not investment advice."]
msg = "\n".join(L)
print(msg)

gh = os.environ.get("GITHUB_STEP_SUMMARY")
if gh:
    with open(gh, "a") as fh:
        fh.write("```\n" + msg + "\n```\n")

tg_t, tg_c = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
if tg_t and tg_c:
    try:
        r = requests.post(f"https://api.telegram.org/bot{tg_t}/sendMessage",
                          json={"chat_id": tg_c, "text": msg, "disable_web_page_preview": True}, timeout=30)
        print("Telegram:", "sent" if r.ok else f"FAILED {r.status_code} {r.text[:150]}")
    except Exception as e:
        print(f"WARNING: Telegram failed -> {e}")
else:
    print("Telegram secrets not set; skipped.")
