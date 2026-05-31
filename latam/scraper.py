"""
scraper.py — LATAM offer-search client (the avianca-style loop, Plan B).

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

Loop model (Akamai's _abck only honors ~2 bare API calls before a 403 because
an idle automated page never feeds the sensor — so we mix two call types):
  - warm_and_capture: a real page navigation resets the _abck budget AND fires
    the page's own search XHR, whose response we capture directly. Used for the
    1st route and every 3rd after (REPLAYS_BETWEEN_WARMS=2), and as fallback
    when a replay gets 403'd. Every navigation earns data — no throwaway calls.
  - replay (search): cheap in-page fetch — mint the token from the offers HTML
    via _fetch_in_page, then hit the search API the same way. The in-page fetch
    runs inside the live page's JS context so Akamai's sensor sees it.

Raws are saved under RAW_DIR in the same {search, response} envelope
avianca uses, so canonical.py can consume them the same way.

Run:  python scraper.py
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

from canonical import canonicalize as canonicalize_payload, load_iata_to_city

# ----- config -----
# Anchor every path to this file's folder so the script works no matter what the
# current working directory is (e.g. run from the repo root or from latam/).
HERE = Path(__file__).resolve().parent
MAPPING_JSON = HERE / "latam_cities.json"
INPUTS_CSV = HERE / "inputs.csv"     # optional; From,To city-keys for the mapping
PROFILE_DIR = HERE / "profile"

# Data outputs live outside the repo, alongside the avianca captures.
DATA_ROOT = Path(r"C:\Users\mike\OneDrive - Prime Time Packaging\flight\latam")
RAW_DIR = DATA_ROOT / "raw"
CANONICAL_DIR = DATA_ROOT / "canonical"
LONGFORMAT_DIR = DATA_ROOT / "longformat"
CAPTURE_DIR = RAW_DIR                 # raws land here
for _d in (RAW_DIR, CANONICAL_DIR, LONGFORMAT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
DEPARTURE_DATE = "2026-06-26"        # one-way; extend inputs with a date col later
POS_COUNTRY = "co"                   # point of sale baked into the URL path
POS_LANG = "es"

# Polite jitter between individual route calls within a sweep (seconds).
MIN_SLEEP = 5
MAX_SLEEP = 15

# Akamai _abck budget: navigate+capture every N replays to reset the sensor.
REPLAYS_BETWEEN_WARMS = 2

SEARCH_ENDPOINT = "/bff/air-offers/v2/offers/search"
# HS512 JWT header in base64 — every search token (and the decoys) starts here.
_JWT_RE = re.compile(r"eyJhbGciOiJIUzUx[\w\-]+\.[\w\-]+\.[\w\-]+")

# Routes used when no inputs.csv is present (testing phase). City-keys into the
# mapping; multi-airport cities (New York -> JFK, LGA) fan out automatically.
DEFAULT_ROUTES = [
    ("Bogota, Colombia", "Los Angeles, United States"),
    ("Bogota, Colombia", "Medellin, Colombia"),
    ("Bogota, Colombia", "New York, United States"),
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
        self.owns_browser = False
        self.replays_since_warm = REPLAYS_BETWEEN_WARMS  # start at threshold → first call warms
        # session-scoped x-latam-* headers harvested during warm_and_capture.
        # The route-bound search-token is NOT kept here — it's minted fresh per route.
        self.base_headers: dict[str, str] = {}

    # ---- lifecycle ----
    def start(self):
        """Standalone: launch our own persistent chrome instance, then warm up."""
        from patchright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(PROFILE_DIR),
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.context.new_page()
        self.owns_browser = True
        # No warm-up here: the first do_one call triggers warm_and_capture
        # (replays_since_warm starts at REPLAYS_BETWEEN_WARMS).
        return self

    def attach(self, page, context):
        """Orchestrator: drive an externally-owned page/context (shared chrome
        instance, one tab per airline). We do NOT own the browser lifecycle."""
        self.page = page
        self.context = context
        self.owns_browser = False
        # No warm-up: first do_one triggers warm_and_capture automatically.
        return self

    def close(self):
        # Only tear down the browser if we created it (standalone mode).
        if not self.owns_browser:
            return
        if self.context:
            self.context.close()
        if self._pw:
            self._pw.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ---- productive warm: reset _abck budget AND capture this route's data ----
    def warm_and_capture(self, origin, dest) -> dict:
        """Navigate to this route's offers page. The navigation runs Akamai's
        sensor (resetting the _abck budget) AND triggers the page's own search
        XHR, whose response we capture directly — so a session refresh doubles as
        a real route result (no throwaway call). Also refreshes session headers."""
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

    # ---- per-route token mint (in-page so Akamai sensor is active) ----
    def _mint_token(self, origin, dest) -> str | None:
        """Fetch the offers HTML in-page → extract the route-correct search token."""
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

    # ---- replay call (cheap, no navigation) ----
    def search(self, origin, dest) -> dict:
        """Mint token + hit search API, both via in-page fetch. Returns {ok,status,body}."""
        token = self._mint_token(origin, dest)
        if not token:
            return {"ok": False, "status": 0, "body": "no route token minted"}
        url = self._search_api_url(origin, dest)
        return self._fetch_in_page(url, self._search_headers(origin, dest, token))

    def _fetch_in_page(self, url, headers=None) -> dict:
        """GET via the live page's own fetch: runs inside the page JS context where
        Akamai's sensor instruments fetch, so cookies stay valid. Returns parsed JSON
        when possible, else raw text (e.g. the offers HTML for token minting)."""
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

    # ---- one planned route: warm_and_capture or replay depending on budget ----
    def do_one(self, row, sweep_num=1, idx=1, total=1):
        """Fire + save a single route. Manages the warm/replay counter internally.
        Shared by standalone sweep() and the orchestrator's interleaved scheduler."""
        fc, tc, origin, dest = row
        print(f"[LA sweep {sweep_num}] [{idx}/{total}] {origin} → {dest}")

        if self.replays_since_warm >= REPLAYS_BETWEEN_WARMS:
            result = self.warm_and_capture(origin, dest)
            self.replays_since_warm = 0
        else:
            result = self.search(origin, dest)
            if not result["ok"] and result.get("status") in (0, 403, 429):
                print(f"   ↻ blocked (status={result['status']}); fallback to navigate+capture")
                result = self.warm_and_capture(origin, dest)
                self.replays_since_warm = 0
            else:
                self.replays_since_warm += 1

        n = None
        body = result.get("body")
        if isinstance(body, dict) and isinstance(body.get("content"), list):
            n = len(body["content"])
        print(f"   ok={result['ok']} status={result['status']} offers={n}")
        path = self.save(fc, tc, origin, dest, result)
        print(f"   💾 {path}")
        return result

    # ---- one full pass over the planned routes ----
    def sweep(self, planned, sweep_num=1):
        self.replays_since_warm = REPLAYS_BETWEEN_WARMS  # force warm on first route
        for i, row in enumerate(planned, 1):
            self.do_one(row, sweep_num, i, len(planned))
            if i < len(planned):
                wait = random.uniform(MIN_SLEEP, MAX_SLEEP)
                print(f"   ⏱ sleeping {wait:.1f}s")
                time.sleep(wait)


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


def load_planned() -> list[tuple]:
    """Read the mapping + inputs.csv and expand into planned (from,to,orig,dest) rows."""
    with open(MAPPING_JSON, encoding="utf-8") as f:
        mapping = json.load(f)
    return load_routes(mapping)


def canonicalize_new():
    """Canonicalize every raw in RAW_DIR that has no canonical yet → CANONICAL_DIR.
    Mirrors avianca's post-sweep step. Error responses (no offers) are skipped."""
    iata_to_city = load_iata_to_city(MAPPING_JSON)
    ok, skipped = 0, 0
    for raw_path in sorted(RAW_DIR.glob("*.json")):
        if raw_path.stem.endswith("_canonical"):
            continue
        out_path = CANONICAL_DIR / f"{raw_path.stem}_canonical.json"
        if out_path.exists():
            continue
        try:
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
            if not payload.get("response", {}).get("ok"):
                skipped += 1
                continue
            out_path.write_text(
                json.dumps(canonicalize_payload(payload, iata_to_city), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            ok += 1
        except Exception:
            skipped += 1
    print(f"   📐 canonicalized: {ok} new" + (f", {skipped} skipped (no data)" if skipped else ""))


def main():
    planned = load_planned()
    if not planned:
        print("Nothing to do — no valid routes.")
        return

    print(f"📋 planned calls: {len(planned)}")
    for i, (fc, tc, o, d) in enumerate(planned, 1):
        print(f"   {i:>2}. {fc} ({o}) → {tc} ({d})")
    print()

    with LatamClient() as client:
        client.sweep(planned)
        canonicalize_new()

    print("\n✅ sweep complete.")


if __name__ == "__main__":
    main()
