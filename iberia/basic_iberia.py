"""
basic_iberia.py — Minimal single-route Iberia availability call (avianca's approach).

Iberia's POST https://ibisservices.iberia.com/api/sse-avm/rs/v2/availability/itinerary
is gated like avianca's air-bounds call:
  - an `authorization: Bearer <JWT>` token (RS256, from Iberia's keycloak; the
    `iberia_web` service account, ~15 min lifetime), and
  - Akamai cookies (_abck, bm_sz, bm_sv, bm_s, ak_bmsc) on the .iberia.com domain.

So we mirror avianca: open an iberia.com page to (a) let Akamai set its cookies and
(b) sniff the Bearer + x-request-* headers off the page's own authed requests, then
replay our own POST via in-page fetch (same-site, credentials included → cookies
ride along). No LATAM-style per-route token/honeypot here.

The exact search-results deep-link that auto-fires an availability call isn't known,
so warm-up just loads the homepage and captures the Bearer from ANY authed
iberia.com request. If the SPA doesn't emit one on its own, the headed browser lets
you run a quick flight search by hand — the listener grabs the token the moment it
appears, then the replay fires automatically.

Run:  python basic_iberia.py
"""

import json
import os
import time
from datetime import datetime

from patchright.sync_api import sync_playwright

# ----- the given flight -----
ORIGIN = "MAD"
DESTINATION = "SAO"
DEPARTURE_DATE = "2026-06-05"      # YYYY-MM-DD
MARKET = "CO"
CABIN = "ECONOMY"
ADULTS = 1

AVAIL_URL = "https://ibisservices.iberia.com/api/sse-avm/rs/v2/availability/itinerary"
WARM_URL = "https://www.iberia.com/co/?language=en"

CAPTURE_DIR = "captures"
# Defaults for the x-request-* headers; overridden by whatever we sniff at warm-up.
DEFAULT_XREQ = {
    "x-request-appversion": "26.08.3",
    "x-request-device": "unknown|chrome|148.0.0.0",
    "x-request-osversion": "windows|windows-10",
    "x-salesforce-token": "not found",
}

os.makedirs(CAPTURE_DIR, exist_ok=True)

# Sniffed at warm-up from the page's own authed requests.
session = {"authorization": None, "xreq": {}}


def stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")


def remember(request):
    """Capture the Bearer token + x-request-* headers from any authed iberia call."""
    if "iberia.com" not in request.url:
        return
    h = request.headers
    auth = h.get("authorization")
    if auth and auth.startswith("Bearer "):
        session["authorization"] = auth
        for k, v in h.items():
            if k.startswith("x-request-") or k == "x-salesforce-token":
                session["xreq"][k] = v


def wait_for_token(page, timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if session["authorization"]:
            return True
        page.wait_for_timeout(500)
    return False


def build_payload():
    return {
        "isPetFlight": False,
        "slices": [{"origin": ORIGIN, "destination": DESTINATION, "date": DEPARTURE_DATE}],
        "passengers": [{"passengerType": "ADULT", "count": str(ADULTS)}],
        "marketCode": MARKET,
        "preferredCabin": CABIN,
    }


def build_headers():
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "authorization": session["authorization"],
        "x-observations-current-page": "availability",
    }
    # Prefer sniffed x-request-* values; fall back to defaults.
    for k, default in DEFAULT_XREQ.items():
        headers[k] = session["xreq"].get(k, default)
    return headers


def call_availability(page, payload, headers):
    """POST via the page's own fetch: same-site, cookies ride along."""
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


def save_result(label, blob):
    path = os.path.join(CAPTURE_DIR, f"{label}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(blob, f, indent=2, ensure_ascii=False)
    return path


def main():
    print(f"🌐 warm-up: {WARM_URL}")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir="iberia_profile",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        page.on("request", remember)

        try:
            page.goto(WARM_URL, wait_until="domcontentloaded")
        except Exception as e:
            print(f"   ⚠ warm-up navigation: {e}")

        print("⏳ waiting for a Bearer token from the page's own calls...")
        if not wait_for_token(page, timeout_s=45):
            print("   ⚠ no token yet — run a flight search in the open window; "
                  "waiting up to 180s more...")
            if not wait_for_token(page, timeout_s=180):
                print("❌ never captured a Bearer token. Aborting.")
                context.close()
                return
        print(f"✅ captured Bearer (+{len(session['xreq'])} x-request headers)")

        payload = build_payload()
        headers = build_headers()
        result = call_availability(page, payload, headers)
        print(f"   ok={result['ok']} status={result['status']}")

        label = f"{stamp()}_{ORIGIN}-{DESTINATION}_{DEPARTURE_DATE}"
        path = save_result(label, {
            "search": {
                "originAirport": ORIGIN,
                "destinationAirport": DESTINATION,
                "departureDate": DEPARTURE_DATE,
                "marketCode": MARKET,
                "cabin": CABIN,
                "queriedAt": datetime.utcnow().isoformat() + "Z",
            },
            "response": result,
            "requestHeaders": {k: v for k, v in headers.items() if k != "authorization"},
        })
        print(f"💾 saved → {path}")

        body = result.get("body")
        if isinstance(body, dict):
            print(f"   response top-level keys: {list(body.keys())}")
        else:
            print("   (non-JSON body — inspect the file)")

        context.close()


if __name__ == "__main__":
    main()
