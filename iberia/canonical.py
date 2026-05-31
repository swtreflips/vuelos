"""
canonical_ib.py — Raw Iberia availability JSON → canonical JSON.

The Iberia analogue of canonical_av.py / canonical_la.py. Output conforms to the
SAME canonical schema (see CLAUDE.md) so the shared aggregators consume it
unchanged; only `airline` and the (preserved) Iberia fare-brand names differ.

Iberia raw shape (per capture from basic_iberia.py), under response.body:
    originDestinations[0]          the searched route (one-way, one OD)
      .origin / .destination       IATA + originCity/destinationCity dicts
      .slices[]                    the itinerary options  → canonical "flights"
        .sliceId, .departureDateTime "YYYY-MM-DD HH:MM", .duration (MINUTES)
        .segments[] {departure{code,city}, arrival{code,city}, flight{...}, ...}
    offers[]                       pricing, normalized OUT of the slices:
      .price {fare, tax, total, currency}
      .originDestinations[].applicableSlices[] {sliceId, segments[]{fareFamily,
        fareBasis, rbd, bookingClass}}     ← links a price to a slice + brand
    fareFamilyConditions[]         commercialCode → commercialName + cabin

So we cross-link: for each slice, gather every offer whose applicableSlices points
at it, key the fareOffers by the fare family's commercialName, keep the cheapest
per (slice, family).

Mapping rules (same philosophy as the other airlines):
  - flightId: SEG-<carrier><flightNumber>-<ORIG><DEST>-<YYYY-MM-DD>-<HHMM>
    (e.g. SEG-IB0271-MADGRU-2026-06-05-1150); itineraryId joins legs with " - ".
  - durations MINUTES → SECONDS (×60); datetimes normalized to ISO
    ("2026-06-05 11:50" → "2026-06-05T11:50:00"); per-segment cities taken from
    the segment itself (Iberia embeds them, so no mapping file needed).
  - fareOffers keyed by Iberia's own brand names (Basic, Optimal, Comfort,
    Flexible, Business Optima/Comfort/Flexible); ALL brands kept. Cross-airline
    tier normalization stays deferred — the `airline` field separates them.
  - Ephemeral / session-scoped data kept for the purchase flow + debugging:
    offerId, fareFamilyCode, fareBasis, rbd, bookingClass, cabin, remainingSeats
    (plus Iberia's fare/tax split). Downstream aggregators drop all but price.

NOTE: assumes one-way single-OD searches (what basic_iberia.py issues). Round-trip
offers span two slices and would need pairing — deferred.

Usage:
    python canonical_ib.py [raw_capture.json]   # default: latest in captures/
"""

import argparse
import json
from pathlib import Path

AIRLINE = "Iberia"
HERE = Path(__file__).resolve().parent

DATA_ROOT     = Path(r"C:\Users\mike\OneDrive - Prime Time Packaging\flight\iberia")
RAW_DIR       = DATA_ROOT / "raw"
CANONICAL_DIR = DATA_ROOT / "canonical"
CAPTURE_DIR   = RAW_DIR


def _iso(dt: str | None) -> str | None:
    """'2026-06-05 11:50' -> '2026-06-05T11:50:00'."""
    if not dt:
        return dt
    dt = dt.replace(" ", "T")
    if len(dt) == 16:          # YYYY-MM-DDTHH:MM, no seconds
        dt += ":00"
    return dt


def _secs(minutes) -> int | None:
    return minutes * 60 if minutes is not None else None


def make_flight_id(carrier: str, number: str, origin: str, dest: str, dep_dt: str) -> str:
    date_part = dep_dt[:10]
    hhmm = dep_dt[11:16].replace(":", "")
    return f"SEG-{carrier}{number}-{origin}{dest}-{date_part}-{hhmm}"


def canonicalize(payload: dict) -> dict:
    search = payload.get("search", {})
    body = payload["response"]["body"]

    families = {f.get("commercialCode"): f for f in body.get("fareFamilyConditions", [])}
    ods = body.get("originDestinations", [])
    od = ods[0] if ods else {}
    slices = {s["sliceId"]: s for s in od.get("slices", [])}

    # ---- cross-link offers onto slices: sliceId -> {familyName: fareOffer} ----
    slice_fares: dict[str, dict] = {sid: {} for sid in slices}
    for offer in body.get("offers", []):
        price = offer.get("price", {})
        for odo in offer.get("originDestinations", []):
            for aps in odo.get("applicableSlices", []):
                sid = aps.get("sliceId")
                if sid not in slice_fares:
                    continue
                segs = aps.get("segments", []) or []
                seg0 = segs[0] if segs else {}
                fam_code = seg0.get("fareFamily")
                fconf = families.get(fam_code, {})
                name = fconf.get("commercialName") or fam_code or "Unknown"
                entry = {
                    "price": price.get("total"),
                    "currency": price.get("currency"),
                    # Iberia uniquely splits the total — keep it, it's free:
                    "fare": price.get("fare"),
                    "tax": price.get("tax"),
                    # ephemeral / session-scoped (purchase flow + debugging):
                    "offerId": offer.get("offerId"),
                    "fareFamilyCode": fam_code,
                    "cabin": fconf.get("cabinDescription"),
                    "bookingClass": seg0.get("bookingClass"),
                    "rbd": seg0.get("rbd"),
                    "fareBasis": seg0.get("fareBasis"),
                    "remainingSeats": aps.get("remainingSeats"),
                }
                prev = slice_fares[sid].get(name)
                # keep the cheapest offer per (slice, family)
                if (prev is None or
                        (entry["price"] is not None and prev.get("price") is not None
                         and entry["price"] < prev["price"])):
                    slice_fares[sid][name] = entry

    # ---- build canonical flights from slices ----
    flights = []
    for sid, sl in slices.items():
        segments_out = []
        for seg in sl.get("segments", []):
            dep = seg.get("departure", {})
            arr = seg.get("arrival", {})
            fl = seg.get("flight", {})
            carrier = (fl.get("marketingCarrier") or {}).get("code", "")
            number = fl.get("marketingFlightNumber", "")
            o = dep.get("code")
            d = arr.get("code")
            dep_dt = seg.get("departureDateTime")
            segments_out.append({
                "flightId": make_flight_id(carrier, number, o, d, dep_dt),
                "aircraftCode": (fl.get("aircraft") or {}).get("code"),
                "originAirport": o,
                "destinationAirport": d,
                "originCity": (dep.get("city") or {}).get("description"),
                "destinationCity": (arr.get("city") or {}).get("description"),
                "departureDateTime": _iso(dep_dt),
                "arrivalDateTime": _iso(seg.get("arrivalDateTime")),
                "durationSeconds": _secs(seg.get("duration")),
            })

        stops = [s["destinationCity"] for s in segments_out[:-1]] if len(segments_out) > 1 else []
        flights.append({
            "itineraryId": " - ".join(s["flightId"] for s in segments_out),
            "segments": segments_out,
            "stops": stops,
            "totalDurationSeconds": _secs(sl.get("duration")),
            "fareOffers": slice_fares.get(sid, {}),
        })

    return {
        "airline": AIRLINE,
        "search": {
            "from": search.get("from") or (od.get("originCity") or {}).get("description"),
            "to": search.get("to") or (od.get("destinationCity") or {}).get("description"),
            "originAirport": search.get("originAirport") or od.get("origin"),
            "destinationAirport": search.get("destinationAirport") or od.get("destination"),
            "departureDate": search.get("departureDate"),
            "queriedAt": search.get("queriedAt"),
        },
        "flights": flights,
    }


def _is_raw(p: Path) -> bool:
    return p.suffix == ".json" and not p.stem.endswith("_canonical")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", help="raw capture json (default: latest in RAW_DIR)")
    args = parser.parse_args()

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

    canonical = canonicalize(payload)
    CANONICAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CANONICAL_DIR / f"{in_path.stem}_canonical.json"
    out_path.write_text(json.dumps(canonical, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(canonical['flights'])} flights to {out_path.name}")


if __name__ == "__main__":
    main()
