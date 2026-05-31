"""
latam_client.py — LATAM offer-search client (the avianca-style loop, Plan B).

Why this shape (see probe results that led here):
  - LATAM's GET /bff/air-offers/v2/offers/search is gated by an HS512 JWT
    `x-latam-search-token` whose payload bakes in origin+destination. The server
    cross-checks it against the query params: replay a BOG-LAX token to BOG-MDE
    and you get HTTP 418. So the token is ROUTE-BOUND — one per route.
  - But the token is server-rendered straight into the offers page HTML, and a
    bare context.request HTML GET (no browser render) returns it once Akamai
    cookies are warm. So we DON'T navigate a page per route.

  The page also salts the HTML with TWO honeypot JWTs (a Yoda quote token and a
  `helloBotToken` token) to trip naive scrapers. We must pick the REAL one — the
  only payload carrying both `country` AND `language` (plus the right route).

Loop model (Akamai's _abck only honors ~2 bare API calls before a 403, because
an idle automated page never feeds the sensor — so we mix two call types):
  - navigate+capture (warm_and_capture): a real page navigation runs Akamai's
    sensor (resetting the budget) AND fires the page's own search XHR, whose
    response we capture directly. Used for the 1st route and every 3rd after it,
    and as the fallback whenever a replay gets 403'd. The refresh is productive —
    it returns a real route's data instead of a throwaway warm call.
  - replay (search): cheap in-page fetch — mint the route token from the offers
    HTML, then call the search API. Used for the 2 routes between refreshes.

Raws are saved under ./captures/ in the same {search, response} envelope
avianca uses, so a future canonical_la.py can consume them the same way.

Run:  python latam_client.py
"""

import base64
import csv
import json
import os
import random
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from patchright.sync_api import sync_playwright

# ----- config -----
# Anchor every path to this file's folder so the script works no matter what the
# current working directory is (e.g. run from the repo root or from latam/).
HERE = Path(__file__).resolve().parent
MAPPING_JSON = HERE / "latam_airport_mapping.json"
INPUTS_CSV = HERE / "inputs.csv"     # optional; From,To city-keys for the mapping
CAPTURE_DIR = HERE / "captures"
PROFILE_DIR = HERE / "latam_profile"
DEPARTURE_DATE = "2026-06-26"        # one-way; extend inputs with a date col later
POS_COUNTRY = "co"                   # point of sale baked into the URL path
POS_LANG = "es"

# Polite jitter between individual route calls within a sweep (seconds).
MIN_SLEEP = 5
MAX_SLEEP = 15

# Cheap "replay" calls to make between productive navigate+capture refreshes.
# Akamai's _abck budget allows ~2 bare API calls before a 403, so we navigate
# (which resets it) every 3rd route and replay the 2 in between.
REPLAYS_BETWEEN_WARMS = 2

SEARCH_ENDPOINT = "/bff/air-offers/v2/offers/search"
# HS512 JWT header in base64 — every search token (and the decoys) starts here.
_JWT_RE = re.compile(r"eyJhbGciOiJIUzUx[\w\-]+\.[\w\-]+\.[\w\-]+")

# Routes used when no inputs.csv is present (testing phase). City-keys into the
# mapping; multi-airport cities (New York -> JFK, LGA) fan out automatically.
DEFAULT_ROUTES = [
    ("Bogota, Colombia", "Santiago de Chile, Chile"),
    ("Bogota, Colombia", "Los Angeles, United States of America"),
    ("Bogota, Colombia", "Cucuta, Colombia"),
    ("Bogota, Colombia", "Medellin, Colombia"),
    ("Bogota, Colombia", "Quito, Ecuador"),
    ("Bogota, Colombia", "Buenos Aires, Argentina"),
    ("Bogota, Colombia", "Cali, Colombia"),
    ("Bogota, Colombia", "Sao Paulo, Brazil")

]


def stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")


def _decode_jwt_payload(tok: str) -> dict:
    try:
        mid = tok.split(".")[1]
        mid += "=" * (-len(mid) % 4)
        return json.loads(base64.urlsafe_b64decode(mid))
    except Exception:
        return {}


def select_route_token(html: str, origin: str, dest: str) -> str | None:
    """Pick the REAL x-latam-search-token out of the page HTML, dodging the
    honeypots. The genuine token's payload carries country+language and the
    matching origin/destination; the decoys carry neither country nor language
    (one is a Yoda quote, the other a `helloBotToken`)."""
    for raw in _JWT_RE.findall(html):
        p = _decode_jwt_payload(raw)
        if (
            "country" in p and "language" in p
            and p.get("origin") == origin and p.get("destination") == dest
        ):
            return raw
    return None


class LatamClient:
    def __init__(self, date=DEPARTURE_DATE, headless=False, capture_dir=CAPTURE_DIR):
        self.date = date
        self.headless = headless
        self.capture_dir = capture_dir
        os.makedirs(capture_dir, exist_ok=True)
        self._pw = None
        self.context = None
        self.page = None
        # session-scoped x-latam-* headers harvested at warm-up (captcha token,
        # app-session-id, application-*). The search-token is NOT kept here —
        # it's minted fresh per route.
        self.base_headers: dict[str, str] = {}

    # ---- lifecycle ----
    def start(self):
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(PROFILE_DIR),
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.context.new_page()
        # No fixed warm-up here: the main loop warms *productively* by navigating
        # to a real route (warm_and_capture), not a throwaway EZE call.
        return self

    def close(self):
        if self.context:
            self.context.close()
        if self._pw:
            self._pw.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ---- productive warm: refresh session AND capture this route ----
    def warm_and_capture(self, origin, dest) -> dict:
        """Navigate to this route's offers page. The navigation runs Akamai's
        sensor (resetting the _abck budget) AND triggers the page's own search
        XHR, whose response we capture directly — so a session refresh doubles as
        a real route result (no throwaway call, no bare GETs to get 403'd). Also
        refreshes the session-scoped x-latam-* headers used by later replays.
        Returns {ok, status, body}."""
        grabbed: dict[str, str] = {}

        def on_request(req):
            if SEARCH_ENDPOINT in req.url:
                for k, v in req.headers.items():
                    if k.startswith("x-latam-"):
                        grabbed[k] = v

        self.page.on("request", on_request)
        url = self._offers_url(origin, dest)
        print(f"   🔥 navigate+capture {origin}->{dest} (session refresh)")
        result = {"ok": False, "status": 0, "body": "no search response"}
        try:
            with self.page.expect_response(
                lambda r: SEARCH_ENDPOINT in r.url, timeout=60_000
            ) as resp_info:
                self.page.goto(url, wait_until="domcontentloaded")
            resp = resp_info.value
            try:
                body = resp.json()
            except Exception:
                body = resp.text()
            result = {"ok": resp.ok, "status": resp.status, "body": body}
        except Exception as e:
            print(f"   ⚠ navigate+capture saw no search response: {e}")
        finally:
            self.page.remove_listener("request", on_request)

        # Refresh session-scoped headers (drop the route-bound token).
        grabbed.pop("x-latam-search-token", None)
        if grabbed:
            self.base_headers = grabbed
        return result

    # ---- url builders ----
    def _offers_url(self, origin, dest) -> str:
        return (
            f"https://www.latamairlines.com/{POS_COUNTRY}/{POS_LANG}/ofertas-vuelos?"
            f"origin={origin}&outbound={self.date}T00:00:00.000Z&destination={dest}"
            f"&adt=1&chd=0&inf=0&trip=OW&cabin=Economy&redemption=false&sort=RECOMMENDED"
        )

    def _search_api_url(self, origin, dest) -> str:
        params = {
            "infant": 0, "utm_term": "undefined", "child": 0, "origin": origin,
            "inOfferId": "null", "locale": f"{POS_LANG}-{POS_COUNTRY}",
            "sort": "RECOMMENDED", "utm_campaign": "undefined", "cabinType": "Economy",
            "utm_content": "undefined", "utm_source": "undefined", "adult": 1,
            "inFlightDate": "null", "destination": dest, "inFrom": "null",
            "outFrom": self.date, "outFlightDate": "null", "kayakclickid": "undefined",
            "outOfferId": "null", "utm_medium": "undefined", "idMetasearch": "undefined",
            "redemption": "false",
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"https://www.latamairlines.com/bff/air-offers/v2/offers/search?{qs}"

    # ---- per-route token mint ----
    def _mint_token(self, origin, dest) -> str | None:
        """Fetch the offers HTML (no render) IN-PAGE so Akamai's sensor stays
        active, then extract the route-correct token. Returns None on block."""
        res = self._fetch_in_page(self._offers_url(origin, dest))
        if not res["ok"] or not isinstance(res["body"], str):
            print(f"   ⚠ mint HTML fetch status={res['status']}")
            return None
        return select_route_token(res["body"], origin, dest)

    def _search_headers(self, origin, dest, token) -> dict:
        h = dict(self.base_headers)            # session-scoped (captcha, session-id…)
        h["x-latam-search-token"] = token       # fresh, route-bound
        h["x-latam-request-id"] = str(uuid.uuid4())
        h["x-latam-track-id"] = str(uuid.uuid4())
        h["accept"] = "application/json, text/plain, */*"
        h["referer"] = self._offers_url(origin, dest)
        return h

    # ---- the call (replay path: cheap, no navigation) ----
    def search(self, origin, dest) -> dict:
        """Mint a route token and hit the search API, both via in-page fetch.
        Returns {ok,status,body}. No self-recovery here — the caller decides
        whether a 403 should trigger a navigate+capture fallback."""
        token = self._mint_token(origin, dest)
        if not token:
            return {"ok": False, "status": 0, "body": "no route token minted"}

        url = self._search_api_url(origin, dest)
        headers = self._search_headers(origin, dest, token)
        return self._fetch_in_page(url, headers)

    def _fetch_in_page(self, url, headers=None) -> dict:
        """GET via the live page's own fetch: it runs inside the page JS context
        where Akamai's sensor instruments fetch, so cookies stay valid. (The bare
        context.request client does NOT run the sensor and gets 403'd after a few
        calls — that was the loop failure.) Returns parsed JSON when possible,
        else the raw text (e.g. the offers HTML for token minting)."""
        return self.page.evaluate(
            """async ({ url, headers }) => {
                try {
                    const res = await fetch(url, { method: "GET",
                        credentials: "include", headers: headers || {} });
                    const text = await res.text();
                    let parsed = null; try { parsed = JSON.parse(text); } catch (e) {}
                    return { ok: res.ok, status: res.status,
                             body: parsed !== null ? parsed : text };
                } catch (e) {
                    return { ok: false, status: 0, body: String(e) };
                }
            }""",
            {"url": url, "headers": headers or {}},
        )

    # ---- persistence ----
    def save(self, from_city, to_city, origin, dest, result) -> str:
        label = f"{stamp()}_{origin}-{dest}_{self.date}"
        path = os.path.join(self.capture_dir, f"{label}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "search": {
                    "from": from_city,
                    "to": to_city,
                    "originAirport": origin,
                    "destinationAirport": dest,
                    "departureDate": self.date,
                    "queriedAt": datetime.utcnow().isoformat() + "Z",
                },
                "response": result,
            }, f, indent=2, ensure_ascii=False)
        return path


# ----- route planning (mirrors avianca's mapping fan-out) -----
def as_list(v):
    return v if isinstance(v, list) else [v]


def load_routes(mapping) -> list[tuple]:
    """Read inputs.csv (From,To city-keys) if present, else DEFAULT_ROUTES;
    expand each city pair into every airport combination."""
    if INPUTS_CSV.exists():
        with open(INPUTS_CSV, encoding="utf-8") as f:
            pairs = [(r["From"].strip(), r["To"].strip()) for r in csv.DictReader(f)]
    else:
        pairs = DEFAULT_ROUTES

    planned = []
    for from_city, to_city in pairs:
        if from_city not in mapping:
            print(f"⚠ unknown 'From' city: {from_city!r}")
            continue
        if to_city not in mapping:
            print(f"⚠ unknown 'To' city: {to_city!r}")
            continue
        for fa in as_list(mapping[from_city]):
            for ta in as_list(mapping[to_city]):
                planned.append((from_city, to_city, fa["iata"], ta["iata"]))
    return planned


def main():
    with open(MAPPING_JSON, encoding="utf-8") as f:
        mapping = json.load(f)

    planned = load_routes(mapping)
    if not planned:
        print("Nothing to do — no valid routes.")
        return

    print(f"📋 planned calls: {len(planned)}")
    for i, (fc, tc, o, d) in enumerate(planned, 1):
        print(f"   {i:>2}. {fc} ({o}) → {tc} ({d})")
    print()

    with LatamClient() as client:
        # Start at the threshold so the very first route is a navigate+capture
        # (the session is cold and needs a real navigation anyway).
        replays_since_warm = REPLAYS_BETWEEN_WARMS
        for i, (fc, tc, origin, dest) in enumerate(planned, 1):
            print(f"[{i}/{len(planned)}] {origin} → {dest}")

            if replays_since_warm >= REPLAYS_BETWEEN_WARMS:
                # Productive refresh: navigation resets the _abck budget AND
                # returns this route's data.
                result = client.warm_and_capture(origin, dest)
                replays_since_warm = 0
            else:
                result = client.search(origin, dest)          # cheap replay
                if not result["ok"] and result.get("status") in (0, 403, 429):
                    print(f"   ↻ blocked (status={result['status']}); navigate+capture fallback")
                    result = client.warm_and_capture(origin, dest)
                    replays_since_warm = 0
                else:
                    replays_since_warm += 1

            n = None
            body = result.get("body")
            if isinstance(body, dict) and isinstance(body.get("content"), list):
                n = len(body["content"])
            print(f"   ok={result['ok']} status={result['status']} offers={n}")
            path = client.save(fc, tc, origin, dest, result)
            print(f"   💾 {path}")

            if i < len(planned):
                wait = random.uniform(MIN_SLEEP, MAX_SLEEP)
                print(f"   ⏱ sleeping {wait:.1f}s")
                time.sleep(wait)

    print("\n✅ sweep complete.")


if __name__ == "__main__":
    main()
