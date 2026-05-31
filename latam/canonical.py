"""
canonical.py — Raw LATAM offer-search JSON → canonical JSON.

The LATAM analogue of avianca/canonical.py. Output conforms to the SAME canonical
schema (see CLAUDE.md) so the existing aggregators consume it unchanged; only
the `airline` value and the (preserved) LATAM fare-brand names differ.

LATAM raw shape (per capture from scraper.py):
    response.body.content[]            one entry per itinerary option
      .summary                         trip-level: duration(min), brands[], cities
      .itinerary[]                     the flight segments
      .newPrices[]                     fare/taxes/total, parallel to summary.brands

Key mappings / decisions:
  - flightId: SEG-<airlineCode><flightNumber>-<ORIG><DEST>-<YYYY-MM-DD>-<HHMM>,
    e.g. SEG-LA4000-BOGMDE-2026-06-26-0515 — same stable scheme as avianca, the
    join key for time-series tracking. itineraryId joins a multi-leg offer's
    segment ids with " - ".
  - Durations are MINUTES in LATAM's payload; canonical stores SECONDS (×60).
  - Per-segment city names aren't in LATAM's itinerary (only IATA), so we fill
    originCity/destinationCity from latam_cities.json (IATA→city).
  - Fare tiers keep LATAM's own brand names, title-cased (Basic, Light, Full,
    Premium Economy Standard, Business, ...). Cross-airline tier normalization
    is deliberately deferred (CLAUDE.md); the `airline` field keeps them
    distinct in the shared store. ALL brands are kept (every cabin).
  - Ephemeral, session-scoped data kept for the eventual purchase flow / debug:
    `offerId` (LATAM's short-lived per-brand booking token) plus `fareBasis`,
    `cabin`, `brandCode`. Downstream aggregators drop these.

Usage:
    python canonical.py [raw_capture.json]
    # no arg → canonicalizes the most recent raw in RAW_DIR
"""

import argparse
import json
from pathlib import Path

AIRLINE = "LATAM"
HERE = Path(__file__).resolve().parent
MAPPING_JSON = HERE / "latam_cities.json"

DATA_ROOT = Path(r"C:\Users\mike\OneDrive - Prime Time Packaging\flight\latam")
RAW_DIR = DATA_ROOT / "raw"
CANONICAL_DIR = DATA_ROOT / "canonical"
CAPTURE_DIR = RAW_DIR


def load_iata_to_city(path: Path) -> dict:
    """Invert latam_cities.json into IATA → city for per-segment naming."""
    mapping = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for airports in mapping.values():
        for a in (airports if isinstance(airports, list) else [airports]):
            out[a["iata"]] = a["city"]
    return out


def make_flight_id(flight: dict, origin: str, dest: str, departure: str) -> str:
    """SEG-LA4000-BOGMDE-2026-06-26-0515 — encodes carrier+number+route+date+time."""
    code = f"{flight.get('airlineCode', '')}{flight.get('flightNumber', '')}"
    date_part = departure[:10]
    hhmm = departure[11:16].replace(":", "")
    return f"SEG-{code}-{origin}{dest}-{date_part}-{hhmm}"


def canonicalize(payload: dict, iata_to_city: dict) -> dict:
    search = payload["search"]
    body = payload["response"]["body"]
    offers = body.get("content", []) if isinstance(body, dict) else []

    def city(code):
        return iata_to_city.get(code, code)

    flights = []
    for offer in offers:
        summary = offer.get("summary", {})
        itinerary = offer.get("itinerary", [])

        segments_out = []
        for seg in itinerary:
            f = seg.get("flight", {})
            origin = seg["origin"]
            dest = seg["destination"]
            dep = seg["departure"]
            dur = seg.get("duration")
            segments_out.append({
                "flightId": make_flight_id(f, origin, dest, dep),
                "aircraftCode": seg.get("equipment"),
                "originAirport": origin,
                "destinationAirport": dest,
                "originCity": city(origin),
                "destinationCity": city(dest),
                "departureDateTime": dep,
                "arrivalDateTime": seg.get("arrival"),
                "durationSeconds": dur * 60 if dur is not None else None,
            })

        stops = [s["destinationCity"] for s in segments_out[:-1]] if len(segments_out) > 1 else []
        total_dur = summary.get("duration")

        fare_offers = {}
        for brand in summary.get("brands", []):
            name = (brand.get("brandText") or "").title()
            if not name:
                continue
            price = brand.get("price", {})
            fare_offers[name] = {
                "price": price.get("amount"),
                "currency": price.get("currency"),
                # ephemeral / session-scoped — for purchase flow + debugging:
                "offerId": brand.get("offerId"),
                "fareBasis": brand.get("farebasis"),
                "cabin": (brand.get("cabin") or {}).get("label"),
                "brandCode": brand.get("id"),
            }

        flights.append({
            "itineraryId": " - ".join(s["flightId"] for s in segments_out),
            "segments": segments_out,
            "stops": stops,
            "totalDurationSeconds": total_dur * 60 if total_dur is not None else None,
            "fareOffers": fare_offers,
        })

    return {
        "airline": AIRLINE,
        "search": {
            "from": search.get("from"),
            "to": search.get("to"),
            "originAirport": search.get("originAirport"),
            "destinationAirport": search.get("destinationAirport"),
            "departureDate": search.get("departureDate"),
            "queriedAt": search.get("queriedAt"),
        },
        "flights": flights,
    }


def _is_raw(p: Path) -> bool:
    return p.suffix == ".json" and not p.stem.endswith("_canonical") and "REPLAY" not in p.stem


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", help="raw capture json (default: latest in RAW_DIR)")
    args = parser.parse_args()

    iata_to_city = load_iata_to_city(MAPPING_JSON)

    if args.input:
        in_path = Path(args.input)
    else:
        raws = sorted(p for p in CAPTURE_DIR.glob("*.json") if _is_raw(p))
        if not raws:
            print(f"No raw captures found in {RAW_DIR}.")
            return
        in_path = raws[-1]

    payload = json.loads(in_path.read_text(encoding="utf-8"))
    resp = payload.get("response", {})
    if not resp.get("ok"):
        print(f"Skipping {in_path.name}: response not ok (status={resp.get('status')}).")
        return

    canonical = canonicalize(payload, iata_to_city)
    CANONICAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CANONICAL_DIR / f"{in_path.stem}_canonical.json"
    out_path.write_text(json.dumps(canonical, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(canonical['flights'])} flights to {out_path.name}")


if __name__ == "__main__":
    main()
