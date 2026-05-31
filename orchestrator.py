"""
orchestrator.py — drive every airline scraper from ONE python process inside ONE
chrome instance, one tab per airline.

Design:
  - A single persistent chromium context is launched here. Each airline gets its
    own page (tab) in that same context; cookies are domain-scoped so avianca's
    auth and LATAM's Akamai session never collide.
  - Both airlines are swept CONCURRENTLY via a single-threaded round-robin
    scheduler (sync Playwright is not thread-safe, so true threads can't share
    one browser — we interleave instead). The first avianca call and the first
    LATAM call fire back-to-back (milliseconds apart). After that, each airline's
    own jitter governs only ITS next call; neither waits on the other. The
    scheduler sleeps only until the soonest airline is ready again.
  - After each sweep both airlines canonicalize their new raws. Between sweeps we
    re-warm both sessions and honor avianca's night/day cadence.

Run:  python orchestrator.py
"""

import importlib.util
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))  # so the airline modules find shared utils.py

from utils import ensure_connected


def load_airline(name):
    """Load <name>/scraper.py as a uniquely-named module. Each airline ships its
    own canonical.py; we pop sys.modules['canonical'] around the load so a
    sibling's canonical can never leak into the next airline."""
    d = ROOT / name
    sys.path.insert(0, str(d))
    sys.modules.pop("canonical", None)
    spec = importlib.util.spec_from_file_location(f"{name}_scraper", d / "scraper.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    if str(d) in sys.path:
        sys.path.remove(str(d))
    sys.modules.pop("canonical", None)
    return mod


av = load_airline("avianca")
la = load_airline("latam")

PROFILE_DIR = ROOT / "orchestrator_profile"
MAX_RUNTIME = timedelta(hours=23, minutes=45)


class Runner:
    """One airline's progress through a single sweep, with its own jitter clock."""

    def __init__(self, tag, planned, step, jitter):
        self.tag = tag
        self.planned = planned
        self.step = step          # step(row, idx, total) -> fires one call
        self.jitter = jitter      # () -> seconds to wait before THIS airline's next call
        self.idx = 0
        self.next_fire = 0.0      # monotonic deadline; 0 == fire now

    def pending(self):
        return self.idx < len(self.planned)

    def fire(self):
        row = self.planned[self.idx]
        self.idx += 1
        try:
            self.step(row, self.idx, len(self.planned))
        except Exception as e:
            print(f"   ⚠ [{self.tag}] call failed: {e!r}")
        self.next_fire = time.monotonic() + self.jitter()


def parallel_sweep(runners):
    """Round-robin: fire every airline that's due, then sleep only until the next
    one is due. First pass fires all airlines immediately (next_fire reset to 0)."""
    for r in runners:
        r.idx = 0
        r.next_fire = 0.0
    while any(r.pending() for r in runners):
        now = time.monotonic()
        fired = False
        for r in runners:
            if r.pending() and now >= r.next_fire:
                r.fire()
                fired = True
        if not fired:
            due = [r.next_fire for r in runners if r.pending()]
            if due:
                time.sleep(max(0.0, min(due) - time.monotonic()))


def main():
    from patchright.sync_api import sync_playwright

    av_planned = av.load_planned()
    la_planned = la.load_planned()
    print(f"📋 avianca: {len(av_planned)} calls   latam: {len(la_planned)} calls")
    if not av_planned and not la_planned:
        print("Nothing to do.")
        return

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        av_page = context.new_page()
        la_page = context.new_page()

        print("\n🌐 warming up avianca tab ...")
        if not av.init_page(av_page):
            print("   ⚠ avianca: no auth headers captured (calls may fail).")
        print("🌐 attaching latam tab (first do_one will warm_and_capture) ...")
        la_client = la.LatamClient()
        la_client.attach(la_page, context)

        start_time = datetime.now()
        sweep_num = 0
        try:
            while True:
                now = datetime.now()
                if now - start_time >= MAX_RUNTIME:
                    print("\n🛑 24h runtime reached. Exiting cleanly.")
                    break

                sweep_num += 1
                ensure_connected()  # single connectivity gate for the process

                if sweep_num > 1:
                    print(f"\n♻ re-warming both sessions before sweep {sweep_num}...")
                    av.rewarm_session(av_page)
                    la_client.replays_since_warm = la.REPLAYS_BETWEEN_WARMS

                print(f"\n🔁 sweep {sweep_num} starting at {now.strftime('%Y-%m-%d %H:%M:%S')}")
                runners = [
                    Runner("AV", av_planned,
                           lambda row, i, n: av.do_one_call(av_page, context, row, sweep_num, i, n),
                           lambda: random.uniform(av.MIN_SLEEP, av.MAX_SLEEP)),
                    Runner("LA", la_planned,
                           lambda row, i, n: la_client.do_one(row, sweep_num, i, n),
                           lambda: random.uniform(la.MIN_SLEEP, la.MAX_SLEEP)),
                ]
                parallel_sweep(runners)

                print("\n📐 canonicalizing new raws ...")
                av.canonicalize_new()
                la.canonicalize_new()

                now = datetime.now()
                if now - start_time >= MAX_RUNTIME:
                    print("\n🛑 24h runtime reached after sweep. Exiting cleanly.")
                    break

                inter_sleep = av.compute_next_sleep(now)
                remaining = (start_time + MAX_RUNTIME) - now
                if remaining.total_seconds() <= inter_sleep:
                    print(f"\n🛑 Remaining budget ({remaining}) ≤ next sleep ({inter_sleep}s). Exiting.")
                    break

                next_at = now + timedelta(seconds=inter_sleep)
                window = "night" if (now.hour >= 22 or now.hour < 6) else "day"
                print(f"\n⏳ {window} window — sleeping {inter_sleep/60:.0f} min until {next_at.strftime('%H:%M:%S')}")
                time.sleep(inter_sleep)
        except KeyboardInterrupt:
            print("\n🛑 Interrupted manually.")
        finally:
            print(f"\n✅ Done. Completed {sweep_num} sweep(s).")
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
