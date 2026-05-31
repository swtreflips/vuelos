"""
scraper.py — Iberia availability searches driven by inputs.csv.

Iberia mirrors avianca's approach:
  - Open iberia.com to let Akamai set cookies and sniff the Bearer JWT +
    x-request-* headers from the page's own authed requests.
  - Replay our own POST to the availability endpoint via in-page fetch
    (same-site, credentials included — cookies ride along automatically).
  - No per-route token minting needed (unlike LATAM).

Bearer token lifetime is ~15 min, so rewarm_session() re-navigates the
warm-up URL before each subsequent sweep to get a fresh one. If the page
doesn't auto-fire an authed call within 45s (e.g. first cold run), the
browser window stays open so the user can run a manual search — the
listener grabs the token the moment it appears.

Run:  python scraper.py
"""

import csv
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # vuelos/ → utils
sys.path.insert(0, str(HERE))           # iberia/ → canonical

from utils import ensure_connected
from canonical import canonicalize

# ----- paths -----
MAPPING_JSON   = HERE / "iberia_cities.json"
INPUTS_CSV     = HERE / "inputs.csv"
PROFILE_DIR    = HERE / "profile"
DATA_ROOT      = Path(r"C:\Users\mike\OneDrive - Prime Time Packaging\flight\iberia")
RAW_DIR        = DATA_ROOT / "raw"
CANONICAL_DIR  = DATA_ROOT / "canonical"
LONGFORMAT_DIR = DATA_ROOT / "longformat"

for _d in (RAW_DIR, CANONICAL_DIR, LONGFORMAT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ----- config -----
AVAIL_URL      = "https://ibisservices.iberia.com/api/sse-avm/rs/v2/availability/itinerary"
WARM_URL       = "https://www.iberia.com/co/?language=en"
REWARM_URLS = [
    # MAD → AMS
    (
        "https://www.iberia.com/flights/?market=CO&language=es&appliesOMB=false"
        "&splitEndCity=false&initializedOMB=true&flexible=true&TRIP_TYPE=1"
        "&BEGIN_CITY_01=MAD&END_CITY_01=AMS&nombreOrigen=Madrid&nombreDestino=Amsterdam"
        "&BEGIN_DAY_01=26&BEGIN_MONTH_01=202606&BEGIN_YEAR_01=2026"
        "&END_DAY_01=&END_MONTH_01=&END_YEAR_01=&FARE_TYPE=undefined&quadrigam=IBHMPA"
        "&ADT=1&CHD=0&INF=0&BNN=0&YTH=0&YCD=0&residentCode=&familianumerosa="
        "&BV_UseBVCookie=no&boton=Buscar&bookingMarket=CO#!/availability"
    ),
    # MAD → LHR  (different destination forces a genuine fresh search, avoids cache)
    (
        "https://www.iberia.com/flights/?market=CO&language=es&appliesOMB=false"
        "&splitEndCity=false&initializedOMB=true&flexible=true&TRIP_TYPE=1"
        "&BEGIN_CITY_01=MAD&END_CITY_01=LHR&nombreOrigen=Madrid&nombreDestino=London"
        "&BEGIN_DAY_01=26&BEGIN_MONTH_01=202606&BEGIN_YEAR_01=2026"
        "&END_DAY_01=&END_MONTH_01=&END_YEAR_01=&FARE_TYPE=undefined&quadrigam=IBHMPA"
        "&ADT=1&CHD=0&INF=0&BNN=0&YTH=0&YCD=0&residentCode=&familianumerosa="
        "&BV_UseBVCookie=no&boton=Buscar&bookingMarket=CO#!/availability"
    ),
]
REWARM_URL = REWARM_URLS[0]   # kept for init_page reference
DEPARTURE_DATE = "2026-06-26"
MARKET         = "CO"
CABIN          = "ECONOMY"

MIN_SLEEP   = 3
MAX_SLEEP   = 8
NIGHT_SLEEP = 80 * 60
DAY_SLEEP   = 3 * 60 * 60
MAX_RUNTIME = timedelta(hours=23, minutes=45)

REPLAYS_BETWEEN_WARMS = 5   # rewarm every 5 successful calls

# Fallback x-request-* values; overridden by whatever is sniffed at warm-up.
DEFAULT_XREQ = {
    "x-request-appversion": "26.08.3",
    "x-request-device": "unknown|chrome|148.0.0.0",
    "x-request-osversion": "windows|windows-10",
    "x-salesforce-token": "not found",
}

DEFAULT_ROUTES = [
    ("Bogota, Colombia", "Madrid, Spain"),
]

# Session state populated by remember_headers() at warm-up.
# Stores "authorization" (Bearer) + "x-request-*" + "x-salesforce-token".
session_headers:     dict[str, str] = {}
_rewarm_url_idx:     int = 0   # alternates between REWARM_URLS on each rewarm
_replays_since_warm: int = 0


def stamp() -> str:
    COT = timezone(timedelta(hours=-5))
    return datetime.now(COT).strftime("%Y%m%d-%H%M%S-%f")


def remember_headers(request):
    if "iberia.com" not in request.url:
        return
    h = request.headers
    auth = h.get("authorization", "")
    if auth.startswith("Bearer "):
        session_headers["authorization"] = auth
        for k, v in h.items():
            if k.startswith("x-request-") or k == "x-salesforce-token":
                session_headers[k] = v


def wait_for_token(page, timeout_s: int = 45) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if session_headers.get("authorization"):
            return True
        page.wait_for_timeout(500)
    return False


def build_headers() -> dict:
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "x-observations-current-page": "availability",
    }
    if "authorization" in session_headers:
        headers["authorization"] = session_headers["authorization"]
    for k, default in DEFAULT_XREQ.items():
        headers[k] = session_headers.get(k, default)
    return headers


def build_payload(origin, dest, departure_date):
    return {
        "isPetFlight": False,
        "slices": [{"origin": origin, "destination": dest, "date": departure_date}],
        "passengers": [{"passengerType": "ADULT", "count": "1"}],
        "marketCode": MARKET,
        "preferredCabin": CABIN,
    }


def call_availability(page, payload):
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
                return { ok: res.ok, status: res.status,
                         body: parsed !== null ? parsed : text };
            } catch (e) {
                return { ok: false, status: 0, body: String(e), networkError: true };
            }
        }""",
        {"url": AVAIL_URL, "headers": headers, "body": payload},
    )


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
    if INPUTS_CSV.exists():
        with open(INPUTS_CSV, encoding="utf-8") as f:
            pairs = [(r["From"].strip(), r["To"].strip()) for r in csv.DictReader(f)]
    else:
        pairs = DEFAULT_ROUTES
    planned = []
    for from_city, to_city in pairs:
        for from_apt, to_apt in expand_pairs(from_city, to_city, mapping):
            planned.append((from_city, to_city, from_apt, to_apt))
    return planned


def save_result(label, blob):
    path = RAW_DIR / f"{label}.json"
    path.write_text(json.dumps(blob, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def canonicalize_new():
    ok, failed = 0, []
    for raw_path in sorted(RAW_DIR.glob("*.json")):
        if raw_path.stem.endswith("_canonical"):
            continue
        out_path = CANONICAL_DIR / f"{raw_path.stem}_canonical.json"
        if out_path.exists():
            continue
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            if not raw.get("response", {}).get("ok"):
                failed.append(raw_path.name)
                continue
            out_path.write_text(
                json.dumps(canonicalize(raw), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            ok += 1
        except Exception:
            failed.append(raw_path.name)
    print(f"   📐 canonicalized: {ok} new" + (f", {len(failed)} skipped (no data)" if failed else ""))


def init_page(page) -> bool:
    """Attach request listener, navigate the search deep-link, wait for Bearer token."""
    page.on("request", remember_headers)
    try:
        page.goto(REWARM_URL, wait_until="domcontentloaded")
    except Exception as e:
        print(f"   ⚠ iberia warm-up navigation: {e}")
    if wait_for_token(page, timeout_s=45):
        return True
    print("   ⚠ re-warm timed out — no fresh Bearer captured.")
    return False


def rewarm_session(page) -> bool:
    """Navigate a fresh search URL (alternating between two destinations) so the
    page fires its own availability call and we capture the Bearer token passively.
    Tries a reload first (faster, uses cached resources); falls back to a fresh
    search URL if the reload doesn't produce a Bearer within 20s."""
    global _rewarm_url_idx
    session_headers.clear()
    try:
        page.reload(wait_until="domcontentloaded")
    except Exception as e:
        print(f"   ⚠ iberia reload failed: {e}")
    if wait_for_token(page, timeout_s=20):
        return True
    # fallback: navigate to alternate search URL
    url = REWARM_URLS[_rewarm_url_idx % len(REWARM_URLS)]
    _rewarm_url_idx += 1
    print(f"   ⚠ reload didn't yield Bearer — navigating {url[-30:]}")
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as e:
        print(f"   ⚠ iberia goto failed: {e}")
    if wait_for_token(page, timeout_s=45):
        return True
    print("   ⚠ rewarm timed out.")
    return False


def do_one_call(page, context, row, sweep_num=1, idx=1, total=1):
    """Fire + save a single planned route. Callable from standalone sweep()
    and from the orchestrator's interleaved scheduler."""
    global _replays_since_warm
    from_city, to_city, from_apt, to_apt = row
    origin = from_apt["code"]
    dest   = to_apt["code"]
    print(f"[IB sweep {sweep_num}] [{idx}/{total}] {from_city} ({origin}) → {to_city} ({dest})")

    if _replays_since_warm >= REPLAYS_BETWEEN_WARMS:
        print("   ♻ rewarm (count reached)")
        rewarm_session(page)
        _replays_since_warm = 0

    payload = build_payload(origin, dest, DEPARTURE_DATE)
    result  = call_availability(page, payload)

    if result.get("status") == 0 or not session_headers.get("authorization"):
        print("   ♻ rewarm + retry (status 0 / no Bearer)")
        rewarm_session(page)
        _replays_since_warm = 0
        result = call_availability(page, payload)

    print(f"   ok={result['ok']} status={result['status']}")

    if result["ok"]:
        _replays_since_warm += 1

    label = f"{stamp()}_{origin}-{dest}_{DEPARTURE_DATE}"
    path  = save_result(label, {
        "search": {
            "from": from_city,
            "to": to_city,
            "originAirport": origin,
            "destinationAirport": dest,
            "departureDate": DEPARTURE_DATE,
            "marketCode": MARKET,
            "queriedAt": datetime.now(timezone(timedelta(hours=-5))).isoformat(),
        },
        "response": result,
    })
    print(f"   💾 {path}")
    return result


def sweep(page, planned, sweep_num):
    for i, row in enumerate(planned, 1):
        do_one_call(page, None, row, sweep_num, i, len(planned))
        if i < len(planned):
            wait = random.uniform(MIN_SLEEP, MAX_SLEEP)
            print(f"   ⏱ sleeping {wait:.1f}s")
            time.sleep(wait)


def compute_next_sleep(now: datetime) -> float:
    return NIGHT_SLEEP if (now.hour >= 22 or now.hour < 6) else DAY_SLEEP


def main():
    from patchright.sync_api import sync_playwright

    planned = load_planned()
    if not planned:
        print("Nothing to do — no valid input rows.")
        return

    print(f"📋 planned calls: {len(planned)}")
    for i, (fc, tc, fa, ta) in enumerate(planned, 1):
        print(f"   {i:>2}. {fc} ({fa['code']}) → {tc} ({ta['code']})")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()

        print(f"\n🌐 Opening iberia warm-up URL ...")
        if not init_page(page):
            print("⚠ Timed out waiting for Bearer token. Aborting.")
            context.close()
            return
        xreq_count = len([k for k in session_headers if k.startswith("x-request-")])
        print(f"✅ Captured Bearer token (+{xreq_count} x-request headers)")

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
                    print(f"\n♻ re-warming iberia session before sweep {sweep_num}...")
                    rewarm_session(page)

                print(f"\n🔁 sweep {sweep_num} starting at {now.strftime('%Y-%m-%d %H:%M:%S')}")
                sweep(page, planned, sweep_num)
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
