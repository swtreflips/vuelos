import json
from pathlib import Path

AIRLINE = "Avianca"
FARE_CODE_TO_NAME = {"LIGHT": "Light", "CLASSIC": "Classic", "FLEX": "Flex"}


def canonicalize(payload):
    search = payload["search"]
    data = payload["response"]["body"]["data"]
    dicts = payload["response"]["body"]["dictionaries"]
    flight_dict = dicts["flight"]
    location_dict = dicts["location"]

    def city(code):
        return location_dict[code]["cityName"].title()

    flights = []
    for group in data["airBoundGroups"]:
        bd = group["boundDetails"]

        segments_out = []
        for seg in bd["segments"]:
            fid = seg["flightId"]
            f = flight_dict[fid]
            dep_code = f["departure"]["locationCode"]
            arr_code = f["arrival"]["locationCode"]
            segments_out.append({
                "flightId": fid,
                "aircraftCode": f["aircraftCode"],
                "originAirport": dep_code,
                "destinationAirport": arr_code,
                "originCity": city(dep_code),
                "destinationCity": city(arr_code),
                "departureDateTime": f["departure"]["dateTime"],
                "arrivalDateTime": f["arrival"]["dateTime"],
                "durationSeconds": f["duration"],
            })

        stops = [s["destinationCity"] for s in segments_out[:-1]] if len(segments_out) > 1 else []

        fare_offers = {}
        for ab in group["airBounds"]:
            name = FARE_CODE_TO_NAME.get(ab["fareFamilyCode"])
            if name is None:
                continue
            price_info = ab["prices"]["totalPrices"][0]
            seg_classes = {a["flightId"]: a["bookingClass"] for a in ab["availabilityDetails"]}
            fare_offers[name] = {
                "price": price_info["total"],
                "currency": price_info["currencyCode"],
                "airBoundId": ab["airBoundId"],
                "segmentBookingClasses": seg_classes,
            }

        flights.append({
            "segments": segments_out,
            "stops": stops,
            "totalDurationSeconds": bd["duration"],
            "fareOffers": fare_offers,
        })

    return {
        "airline": AIRLINE,
        "search": {
            "from": search["from"],
            "to": search["to"],
            "originAirport": search["originAirport"],
            "destinationAirport": search["destinationAirport"],
            "departureDate": search["departureDate"],
            "queriedAt": search["queriedAt"],
        },
        "flights": flights,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = in_path.with_name(f"{in_path.stem}_canonical.json")
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    canonical = canonicalize(payload)
    out_path.write_text(json.dumps(canonical, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(canonical['flights'])} flights to {out_path}")
