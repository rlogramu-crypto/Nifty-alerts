"""Send the end-of-day signals summary for TODAY. Runs after the close (15:35 IST) on its own.
Educational only. Not financial advice. Places NO orders."""
from nifty_scanner_upstox import INDEX_KEY, eod_summary, fetch_candles, get_future_key, send_telegram


def main():
    df = fetch_candles(INDEX_KEY)
    if df.empty:
        send_telegram("No Nifty candles found for today, so no end-of-day summary.")
        return
    fut = None
    try:
        key, _ = get_future_key()
        fut = fetch_candles(key) if key else None
    except Exception as e:
        print(f"Futures lookup failed ({e}); summary will have no futures data.")
    msg = eod_summary(df, fut)
    print(msg)
    send_telegram(msg)


if __name__ == "__main__":
    main()
