"""
avianca/scraper.py — Programmatic Avianca air-bounds searches driven by inputs.csv.

Flow:
  1. Read inputs.csv (columns: From, To — values are "City, Country" keys
     into avianca_cities.json).
  2. Look each city up in the mapping. If a city has multiple airports
     (e.g. Buenos Aires -> EZE + AEP), fan out: one call per airport pair.
  3. Open the booking deep-link to warm up the session and capture the
     Authorization + x-d-token headers from the page's own first call.
  4. Sweep every planned pair using the captured headers, with a small
     random pause between individual calls.
  5. Save every response under RAW_DIR.
  6. Sleep until the next sweep based on the wall clock:
       - 22:00-06:00 (local): every 80 min (6 sweeps over 8h)
       - 06:00-22:00 (local): every 3h (5-6 sweeps over 16h)
     Re-warm the session before each subsequent sweep so auth tokens
     stay fresh. The whole script runs for 24 hours then exits.
"""

import csv
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import check_internet, reset_wifi, ensure_connected
from canonical import canonicalize

# ----- paths -----
HERE = Path(__file__).parent
INPUTS_CSV   = HERE / "inputs.csv"
MAPPING_JSON = HERE / "avianca_cities.json"
PROFILE_DIR  = HERE / "profile"
RAW_DIR      = Path(r"C:\Users\mike\OneDrive - Prime Time Packaging\flight\captures\raw")
CANONICAL_DIR = Path(r"C:\Users\mike\OneDrive - Prime Time Packaging\flight\captures\canonical")

# ----- config -----
BOOKING_URL = (
    "https://booking.avianca.com/av/booking/avail?"
    "departureDate=2026-06-07&tripType=round-trip&platform=WEBB2C"
    "&from=BOG&to=EZE&nbAdults=1&nbYoungs=0&nbChildren=0&nbInfants=0"
    "&language=ES&pointOfSale=CO&returnDate=2026-06-28&promoCode="
    "&FriendlyIDNegoF=&overrides=%7B%22enableFlexCancelTeaser%22:%22true%22"
    ",%22useHPP%22:%22true%22,%22useAvCheckout%22:%22false%22%7D"
    "&accessMethod=default&backend=PRD"
)
AIR_BOUNDS_URL = "https://apibooking.avianca.com/v2/search/air-bounds"
DEPARTURE_DATE = "2026-06-26"

MIN_SLEEP   = 3
MAX_SLEEP   = 8
NIGHT_SLEEP = 80 * 60
DAY_SLEEP   = 3 * 60 * 60
MAX_RUNTIME = timedelta(hours=23, minutes=45)

RAW_DIR.mkdir(parents=True, exist_ok=True)
CANONICAL_DIR.mkdir(parents=True, exist_ok=True)

session_headers: dict[str, str] = {}


def stamp() -> str:
    COT = timezone(timedelta(hours=-5))
    return datetime.now(COT).strftime("%Y%m%d-%H%M%S-%f")


def remember_headers(request):
    if "apibooking.avianca.com" not in request.url:
        return
    h = request.headers
    for k in ("authorization", "x-d-token", "ama-client-ref"):
        if k in h:
            session_headers[k] = h[k]


def build_headers() -> dict:
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://booking.avianca.com",
        "referer": "https://booking.avianca.com/",
    }
    for k in ("authorization", "x-d-token", "ama-client-ref"):
        if k in session_headers:
            headers[k] = session_headers[k]
    return headers


def call_air_bounds_via_page(page, payload):
    headers = build_headers()
    return page.evaluate(
        """async ({ url, headers, body }) => {
            try {
                const res = await fetch(url, {
                    method: "POST",
                    credentials: "include",
                    headers: headers,
                    body: JSON.stringify(body),
                });
                const text = await res.text();
                let parsed = null;
                try { parsed = JSON.parse(text); } catch (e) {}
                return {
                    ok: res.ok,
                    status: res.status,
                    body: parsed !== null ? parsed : text,
                };
            } catch (e) {
                return { ok: false, status: 0, body: String(e), networkError: true };
            }
        }""",
        {"url": AIR_BOUNDS_URL, "headers": headers, "body": payload},
    )


def call_air_bounds_via_context(context, payload):
    headers = build_headers()
    resp = context.request.post(AIR_BOUNDS_URL, headers=headers, data=json.dumps(payload))
    try:
        body = resp.json()
    except Exception:
        body = resp.text()
    return {"ok": resp.ok, "status": resp.status, "body": body}


def call_with_fallback(page, context, payload):
    result = call_air_bounds_via_page(page, payload)
    if not result["ok"]:
        result = call_air_bounds_via_context(context, payload)
    return result


def wait_for_headers(page, timeout_s: int = 45) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if "authorization" in session_headers and "x-d-token" in session_headers:
            return True
        page.wait_for_timeout(500)
    return False


def make_payload(origin_iata, destination_iata, departure_date):
    return {
        "commercialFareFamilies": ["BRANDEDWEB"],
        "itineraries": [
            {
                "departureDateTime": f"{departure_date}T00:00:00.000",
                "originLocationCode": origin_iata,
                "destinationLocationCode": destination_iata,
                "isRequestedBound": True,
            },
        ],
        "travelers": [{"passengerTypeCode": "ADT"}],
    }


def expand_pairs(from_city, to_city, mapping):
    if from_city not in mapping:
        print(f"⚠ skipping row: unknown 'From' city: {from_city!r}")
        return
    if to_city not in mapping:
        print(f"⚠ skipping row: unknown 'To' city: {to_city!r}")
        return
    for from_apt in mapping[from_city]:
        for to_apt in mapping[to_city]:
            yield from_apt, to_apt


def load_planned() -> list:
    with open(MAPPING_JSON, encoding="utf-8") as f:
        mapping = json.load(f)
    with open(INPUTS_CSV, encoding="utf-8") as f:
        rows = [(r["From"].strip(), r["To"].strip()) for r in csv.DictReader(f)]
    planned = []
    for from_city, to_city in rows:
        for from_apt, to_apt in expand_pairs(from_city, to_city, mapping):
            planned.append((from_city, to_city, from_apt, to_apt))
    return planned


def init_page(page) -> bool:
    """Attach request listener, navigate warm-up URL, wait for auth headers."""
    page.on("request", remember_headers)
    page.goto(BOOKING_URL, wait_until="domcontentloaded")
    return wait_for_headers(page, timeout_s=45)


def save_result(label, blob):
    path = RAW_DIR / f"{label}.json"
    path.write_text(json.dumps(blob, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def canonicalize_new():
    ok, failed = 0, []
    for raw_path in sorted(RAW_DIR.glob("*.json")):
        out_path = CANONICAL_DIR / f"{raw_path.stem}_canonical.json"
        if out_path.exists():
            continue
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            out_path.write_text(
                json.dumps(canonicalize(raw), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            ok += 1
        except Exception:
            failed.append(raw_path.name)
    print(f"   📐 canonicalized: {ok} new" + (f", {len(failed)} skipped (no data)" if failed else ""))



def compute_next_sleep(now: datetime) -> float:
    h = now.hour
    if h >= 22 or h < 6:
        return NIGHT_SLEEP
    return DAY_SLEEP


def rewarm_session(page) -> bool:
    session_headers.clear()
    try:
        page.goto(BOOKING_URL, wait_until="domcontentloaded")
    except Exception as e:
        print(f"   ⚠ re-warm navigation failed: {e}")
    if wait_for_headers(page, timeout_s=45):
        return True
    print("   ⚠ re-warm timed out — no fresh headers captured.")
    return False


def do_one_call(page, context, row, sweep_num=1, idx=1, total=1):
    """Fire + save a single planned route. Returns the call result.
    Shared by standalone sweep() and the orchestrator's interleaved scheduler."""
    from_city, to_city, from_apt, to_apt = row
    origin = from_apt["iata"]
    dest   = to_apt["iata"]
    print(f"[AV sweep {sweep_num}] [{idx}/{total}] {from_city} ({origin}) → {to_city} ({dest})")

    payload = make_payload(origin, dest, DEPARTURE_DATE)
    result  = call_with_fallback(page, context, payload)
    print(f"   ok={result['ok']} status={result['status']}")

    label = f"{stamp()}_{origin}-{dest}_{DEPARTURE_DATE}"
    path  = save_result(label, {
        "search": {
            "from": from_city,
            "to": to_city,
            "originAirport": origin,
            "destinationAirport": dest,
            "departureDate": DEPARTURE_DATE,
            "queriedAt": datetime.now(timezone(timedelta(hours=-5))).isoformat(),
        },
        "response": result,
    })
    print(f"   💾 {path}")
    return result


def sweep(page, context, planned, sweep_num):
    for i, row in enumerate(planned, 1):
        do_one_call(page, context, row, sweep_num, i, len(planned))
        if i < len(planned):
            wait = random.uniform(MIN_SLEEP, MAX_SLEEP)
            print(f"   ⏱ sleeping {wait:.1f}s")
            time.sleep(wait)


def main():
    from patchright.sync_api import sync_playwright

    planned = load_planned()
    if not planned:
        print("Nothing to do — no valid input rows.")
        return

    print(f"📋 planned calls: {len(planned)}")
    for i, (fc, tc, fa, ta) in enumerate(planned, 1):
        print(f"   {i:>2}. {fc} ({fa['iata']}) → {tc} ({ta['iata']})")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()

        print(f"\n🌐 Opening warm-up URL ...")
        if not init_page(page):
            print("⚠ Timed out waiting for auth headers. Aborting.")
            context.close()
            return
        print(f"✅ Captured headers: {list(session_headers.keys())}")

        start_time = datetime.now()
        sweep_num  = 0
        try:
            while True:
                now = datetime.now()
                if now - start_time >= MAX_RUNTIME:
                    print("\n🛑 24h runtime reached. Exiting cleanly.")
                    break

                sweep_num += 1
                ensure_connected()
                if sweep_num > 1:
                    print(f"\n♻ re-warming session before sweep {sweep_num}...")
                    rewarm_session(page)

                print(f"\n🔁 sweep {sweep_num} starting at {now.strftime('%Y-%m-%d %H:%M:%S')}")
                sweep(page, context, planned, sweep_num)
                canonicalize_new()

                now = datetime.now()
                if now - start_time >= MAX_RUNTIME:
                    print("\n🛑 24h runtime reached after sweep. Exiting cleanly.")
                    break

                inter_sleep = compute_next_sleep(now)
                remaining   = (start_time + MAX_RUNTIME) - now
                if remaining.total_seconds() <= inter_sleep:
                    print(f"\n🛑 Remaining budget ({remaining}) ≤ next sleep ({inter_sleep}s). Exiting.")
                    break

                next_at = now + timedelta(seconds=inter_sleep)
                window  = "night" if (now.hour >= 22 or now.hour < 6) else "day"
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
