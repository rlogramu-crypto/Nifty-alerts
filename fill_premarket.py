"""
Pulls pre-market numbers from the Upstox API. No other files needed.
Prints a summary (and posts it to the GitHub Actions run page). If an Excel path is
given as an argument and the file exists, it also fills the Pre-Market sheet.

Fills: date, Nifty prev close/high/low, India VIX + 1-day change, PCR,
highest put-OI strike (support), highest call-OI strike (resistance).
Does NOT fill: GIFT Nifty, breadth, FII/DII, US/Asia, Bank Nifty view (type those in).

Setup (once):
  pip install requests
  Upstox Developer Apps -> Analytics tab -> generate Analytics Token (read-only, 1 year)

Run (close the Excel file first):
  Windows (PowerShell):  $env:UPSTOX_TOKEN="your_token"; python fill_premarket.py Options_Trade_Journal.xlsx
  Mac/Linux:             UPSTOX_TOKEN="your_token" python fill_premarket.py Options_Trade_Journal.xlsx
"""
import os, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import requests

BASE = "https://api.upstox.com"
NIFTY = "NSE_INDEX|Nifty 50"
VIX = "NSE_INDEX|India VIX"
IST = ZoneInfo("Asia/Kolkata")

token = os.environ.get("UPSTOX_TOKEN")
if not token:
    sys.exit("Set the UPSTOX_TOKEN environment variable first (do not paste the token into the script).")
HEAD = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

def get(path, params=None):
    r = requests.get(BASE + path, headers=HEAD, params=params, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Upstox error {r.status_code} on {path}: {r.text[:300]}")
    return r.json().get("data")

def daily_candles(key, days=15):
    now = datetime.now(IST)
    to_d = now.strftime("%Y-%m-%d")
    from_d = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    data = get(f"/v3/historical-candle/{quote(key, safe='')}/days/1/{to_d}/{from_d}")
    rows = data["candles"]  # [timestamp, open, high, low, close, volume, oi]
    # keep completed sessions only: drop today's candle unless market has closed (after 15:45 IST)
    cutoff_today = now.hour * 60 + now.minute >= 15 * 60 + 45
    out = []
    for c in rows:
        d = c[0][:10]
        if d == to_d and not cutoff_today:
            continue
        out.append(c)
    out.sort(key=lambda c: c[0], reverse=True)  # newest first
    return out

nifty = daily_candles(NIFTY)
vix = daily_candles(VIX)
if len(nifty) < 1 or len(vix) < 2:
    sys.exit("Not enough candle data returned.")
n = nifty[0]
v0, v1 = vix[0][4], vix[1][4]
print(f"Nifty {n[0][:10]}: close {n[4]}, high {n[2]}, low {n[3]}")
print(f"VIX {v0} ({(v0 / v1 - 1) * 100:+.2f}% 1-day)")

# Nearest expiry
contracts = get("/v2/option/contract", {"instrument_key": NIFTY})
today = datetime.now(IST).strftime("%Y-%m-%d")
expiries = sorted({c["expiry"][:10] for c in contracts if c["expiry"][:10] >= today})
if not expiries:
    sys.exit("No upcoming expiry found.")
expiry = expiries[0]

chain = get("/v2/option/chain", {"instrument_key": NIFTY, "expiry_date": expiry})
put_oi = {}; call_oi = {}
for row in chain:
    k = row["strike_price"]
    call_oi[k] = (row.get("call_options") or {}).get("market_data", {}).get("oi") or 0
    put_oi[k] = (row.get("put_options") or {}).get("market_data", {}).get("oi") or 0
tot_c, tot_p = sum(call_oi.values()), sum(put_oi.values())
pcr = round(tot_p / tot_c, 2) if tot_c else None
sup = max(put_oi, key=put_oi.get)
res = max(call_oi, key=call_oi.get)
print(f"Expiry {expiry}: PCR {pcr}, max put OI {sup}, max call OI {res}")


vchg = v0 / v1 - 1
vsig = "Bullish" if vchg <= -0.03 else "Bearish" if vchg >= 0.03 else "Neutral"
psig = "-" if pcr is None else ("Bullish" if pcr >= 1.2 else "Bearish" if pcr <= 0.8 else "Neutral")
summary = f"""## Pre-market {datetime.now(IST):%d-%b-%Y}
| Item | Value |
|---|---|
| Nifty prev close | {n[4]:.2f} ({n[0][:10]}) |
| Prev high / low | {n[2]:.2f} / {n[3]:.2f} |
| CE trigger (5-min close above) | {n[2]:.2f} or first-15-min high if higher |
| PE trigger (5-min close below) | {n[3]:.2f} or first-15-min low if lower |
| India VIX | {v0:.2f} ({vchg*100:+.2f}%) -> {vsig} |
| PCR (expiry {expiry}) | {pcr} -> {psig} |
| Highest put OI (support) | {sup} |
| Highest call OI (resistance) | {res} |

Still check yourself: GIFT Nifty, breadth, FII/DII, US/Asia, Bank Nifty, events.
Information only, not investment advice.
"""
print(summary)
gh = os.environ.get("GITHUB_STEP_SUMMARY")
if gh:
    with open(gh, "a") as f:
        f.write(summary)

path = sys.argv[1] if len(sys.argv) > 1 else None
if path and os.path.exists(path):
    from openpyxl import load_workbook
    wb = load_workbook(path)
    ws = wb["Pre-Market"]
    ws["B5"] = datetime.now(IST).date()
    ws["B6"] = round(n[4], 2); ws["B7"] = round(n[2], 2); ws["B8"] = round(n[3], 2)
    ws["B10"] = round(v0, 2); ws["B11"] = round(vchg, 4)
    if pcr is not None:
        ws["B13"] = pcr
    ws["B14"] = sup; ws["B15"] = res
    wb.save(path)
    print("Excel sheet updated.")
