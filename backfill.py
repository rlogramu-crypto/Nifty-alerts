"""
Backfill the data/ folder with one CSV per past trading day, in one run (no video, no per-day Telegram files).
Uses the same table as day_file.py, so the month report can read it. Places NO orders. Not financial advice.
Env: START (YYYY-MM-DD), END (blank = START), OVERWRITE (yes = redo days that already exist),
     UPSTOX_ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import subprocess
import time
from datetime import datetime, timedelta

import day_file as D


def save_to_repo(msg):
    """Commit and push data/ so a long run never loses finished days. Ignored if it is not a git checkout."""
    for cmd in (["git", "config", "user.name", "github-actions"],
                ["git", "config", "user.email", "actions@users.noreply.github.com"],
                ["git", "add", "data"]):
        subprocess.run(cmd, check=False)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode != 0:
        subprocess.run(["git", "commit", "-m", msg], check=False)
        subprocess.run(["git", "pull", "--rebase"], check=False)
        subprocess.run(["git", "push"], check=False)


def parse_date(text):
    """Accepts 2026-10-01, 2026/10/1, 2026.10.1 or 2026-10-1."""
    y, m, d = (int(x) for x in text.strip().replace("/", "-").replace(".", "-").split("-"))
    return datetime(y, m, d).date()


def main():
    start = parse_date(os.environ["START"])
    end = parse_date(os.environ.get("END") or os.environ["START"])
    end = min(end, datetime.now(D.IST).date())      # nothing to fetch for future days
    overwrite = os.environ.get("OVERWRITE", "").strip().lower() == "yes"
    key = name = None
    try:
        fk = D.get_future_key()          # v7.1 returns 3 values, v8 returns 2
        key, name = fk[0], fk[1]
    except Exception as e:
        print(f"Futures lookup failed ({e}); days will be saved without volume and VWAP.")
    os.makedirs("data", exist_ok=True)
    done, skipped, failed = [], [], []
    day = start
    while day <= end:
        ds = day.isoformat()
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        path = f"data/nifty_{ds}.csv"
        if os.path.exists(path) and not overwrite:
            skipped.append(f"{ds} (already saved)")
        else:
            try:
                df = D.fetch_day(D.INDEX_KEY, ds)
                if df.empty:
                    skipped.append(f"{ds} (no data: holiday or not available)")
                else:
                    fut = D.fetch_day(key, ds) if key else None
                    D.build_table(df, fut).to_csv(path)
                    done.append(ds)
                    print(f"saved {path}", flush=True)
                    save_to_repo(f"Backfill {ds}")
            except Exception as e:
                failed.append(f"{ds} ({str(e)[:60]})")
                print(f"{ds} failed: {e}", flush=True)
            time.sleep(1)
        day += timedelta(days=1)
    lines = [f"BACKFILL {start} to {end}", f"Saved {len(done)} days" + (f": {', '.join(d[5:] for d in done)}" if done else ""),
             f"Futures: {name or 'none (no volume/VWAP)'}"]
    if skipped:
        lines.append("Skipped: " + "; ".join(skipped))
    if failed:
        lines.append("Failed: " + "; ".join(failed))
    lines.append("Next: run 'Month report' with the month, e.g. 2026-10.")
    D.send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
