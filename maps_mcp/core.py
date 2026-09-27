"""Business logic behind the two MCP tools. Kept free of any MCP/transport
concerns so it can be unit tested and reused by scripts/validate_routes.py.
"""
import math
from datetime import datetime, timedelta, timezone

import googlemaps.convert

from . import google_client as gc
from .cache import ttl_cache
from .formatting import format_distance, format_duration

CACHE_TTL_SECONDS = 300  # 5 minutes, per build spec


def _leg_duration(leg: dict) -> dict:
    """Prefer traffic-adjusted duration when Google returns one."""
    return leg.get("duration_in_traffic", leg["duration"])


@ttl_cache(ttl_seconds=CACHE_TTL_SECONDS)
def compute_drive_time(origin: str, destination: str, departure_time: str = "now") -> dict:
    departure = gc.resolve_departure_time(departure_time)
    route = gc.fetch_directions(origin, destination, departure_time=departure)
    leg = route["legs"][0]
    duration_field = _leg_duration(leg)

    return {
        "duration": format_duration(duration_field["value"]),
        "duration_seconds": duration_field["value"],
        "distance": format_distance(leg["distance"]["value"]),
        "origin": leg["start_address"],
        "destination": leg["end_address"],
        "traffic_adjusted": "duration_in_traffic" in leg,
    }


@ttl_cache(ttl_seconds=CACHE_TTL_SECONDS)
def compute_route_eta(waypoints: list[str], departure_time: str = "now") -> dict:
    if len(waypoints) < 2:
        raise ValueError("waypoints must contain at least an origin and a destination")

    origin, *middle, destination = waypoints
    departure = gc.resolve_departure_time(departure_time)
    route = gc.fetch_directions(origin, destination, departure_time=departure, waypoints=middle)

    total_seconds = 0
    total_meters = 0
    traffic_adjusted = False
    legs = []
    for leg in route["legs"]:
        duration_field = _leg_duration(leg)
        total_seconds += duration_field["value"]
        total_meters += leg["distance"]["value"]
        traffic_adjusted = traffic_adjusted or "duration_in_traffic" in leg
        legs.append(
            {
                "from": leg["start_address"],
                "to": leg["end_address"],
                "duration": format_duration(duration_field["value"]),
                "distance": format_distance(leg["distance"]["value"]),
            }
        )

    arrival_time = departure + timedelta(seconds=total_seconds)

    return {
        "total_duration": format_duration(total_seconds),
        "total_distance": format_distance(total_meters),
        "arrival_time": arrival_time.isoformat(timespec="seconds"),
        "legs": legs,
        "traffic_adjusted": traffic_adjusted,
    }


@ttl_cache(ttl_seconds=CACHE_TTL_SECONDS)
def compute_geocode(address: str) -> dict:
    result = gc.validate_address(address)
    verdict = result.get("verdict", {})
    address_info = result.get("address", {})
    geocode_info = result.get("geocode", {})
    location = geocode_info.get("location", {})

    return {
        "input": address,
        "formatted": address_info.get("formattedAddress"),
        "lat": location.get("latitude"),
        "lng": location.get("longitude"),
        "place_id": geocode_info.get("placeId"),
        "complete": verdict.get("addressComplete", False),
        "unconfirmed_components": address_info.get("unconfirmedComponentTypes", []),
    }


@ttl_cache(ttl_seconds=CACHE_TTL_SECONDS)
def compute_places(
    origin: str,
    destination: str,
    keyword: str,
    place_type: str = "restaurant",
    max_results: int = 3,
    departure_time: str = "now",
) -> dict:
    departure = gc.resolve_departure_time(departure_time)
    scheduled = _is_scheduled(departure_time)
    route = gc.fetch_directions(origin, destination, departure_time=departure)
    direct_seconds = route["legs"][0]["duration"]["value"]
    start_location = route["legs"][0]["start_location"]
    raw_places = gc.places_nearby(start_location, keyword, place_type, open_now=not scheduled)

    candidates = []
    for place in raw_places[: max_results * 3 if scheduled else max_results]:
        to_stop, detour_seconds = _stop_timing(origin, destination, place, departure, direct_seconds)
        candidates.append(
            (
                place,
                to_stop,
                {
                    "name": place["name"],
                    "address": place.get("vicinity"),
                    "rating": place.get("rating"),
                    "detour_minutes": format_duration(detour_seconds),
                    "place_id": place["place_id"],
                },
            )
        )

    return {
        "origin": origin,
        "destination": destination,
        "keyword": keyword,
        "type": place_type,
        "places": _keep_open(candidates, departure, scheduled, max_results),
    }


_NEARBY_KIND_TO_TYPE = {"food": "restaurant", "gas": "gas_station"}
NEARBY_MAX_RESULTS_CAP = 8
_ROUTE_SAMPLE_POINTS = 3
_MINUTES_PER_WEEK = 7 * 24 * 60


def _is_scheduled(departure_time: str | None) -> bool:
    """True when the caller asked about a planned departure rather than now."""
    return departure_time is not None and departure_time.strip().lower() != "now"


def _minute_of_week(day: int, hhmm: str) -> int:
    """Google's (day, "HHMM") with Sunday = 0, as minutes since Sunday 00:00."""
    return day * 24 * 60 + int(hhmm[:2]) * 60 + int(hhmm[2:])


def _is_open_at(details: dict, when: datetime) -> bool | None:
    """Whether a place is open at `when`, from its Place Details hours.

    Prefers current_opening_hours (the next 7 days, including holiday
    changes) over the regular weekly schedule. Returns None when Google has
    no hours for the place.
    """
    hours = details.get("current_opening_hours") or details.get("opening_hours") or {}
    periods = hours.get("periods")
    if not periods or "utc_offset" not in details:
        return None

    local = when.astimezone(timezone(timedelta(minutes=details["utc_offset"])))
    now_minute = _minute_of_week((local.weekday() + 1) % 7, local.strftime("%H%M"))
    for period in periods:
        if "close" not in period:  # Google's encoding for open 24/7
            return True
        opens = _minute_of_week(period["open"]["day"], period["open"]["time"])
        closes = _minute_of_week(period["close"]["day"], period["close"]["time"])
        if closes <= opens:  # wraps past Saturday night
            closes += _MINUTES_PER_WEEK
        for minute in (now_minute, now_minute + _MINUTES_PER_WEEK):
            if opens <= minute < closes:
                return True
    return False


def _keep_open(
    candidates: list[tuple[dict, int, dict]],
    departure: datetime,
    scheduled: bool,
    limit: int,
) -> list[dict]:
    """Take (raw place, seconds to reach it, result dict) candidates in rank
    order and return up to `limit` results that will be open on arrival.

    For "now" searches Places already filtered to open places, so this just
    reports open_now. For a planned departure it checks each place's hours at
    its arrival time, looking up Place Details only until `limit` are found.
    Places with no published hours are kept with open_at_arrival = None.
    """
    results = []
    for place, seconds_to_place, result in candidates:
        if len(results) >= limit:
            break
        if not scheduled:
            result["open_now"] = place.get("opening_hours", {}).get("open_now")
        else:
            arrival = departure + timedelta(seconds=seconds_to_place)
            open_at_arrival = _is_open_at(gc.place_hours(place["place_id"]), arrival)
            if open_at_arrival is False:
                continue
            result["arrival_time"] = arrival.isoformat(timespec="minutes")
            result["open_at_arrival"] = open_at_arrival
        results.append(result)
    return results


def _stop_timing(
    origin: str, destination: str, place: dict, departure, direct_seconds: int
) -> tuple[int, int]:
    """Route origin -> place -> destination and return (seconds from origin to
    the place, extra seconds the stop adds over the direct route).

    Both sides use plain `duration`: Google omits duration_in_traffic on
    routes with stopover waypoints, so mixing the two would skew the detour.
    """
    place_location = place["geometry"]["location"]
    legs = gc.fetch_directions(
        origin,
        destination,
        departure_time=departure,
        waypoints=[f"{place_location['lat']},{place_location['lng']}"],
    )["legs"]
    to_stop = legs[0]["duration"]["value"]
    via_total = sum(leg["duration"]["value"] for leg in legs)
    return to_stop, max(0, via_total - direct_seconds)


def _interpolate(points: list[dict], fraction: float) -> dict:
    """Return the lat/lng `fraction` of the way along a polyline's length."""
    if len(points) == 1:
        return points[0]
    segments = [math.dist((a["lat"], a["lng"]), (b["lat"], b["lng"])) for a, b in zip(points, points[1:])]
    remaining = sum(segments) * fraction
    for (a, b), length in zip(zip(points, points[1:]), segments):
        if remaining <= length and length > 0:
            t = remaining / length
            return {"lat": a["lat"] + (b["lat"] - a["lat"]) * t, "lng": a["lng"] + (b["lng"] - a["lng"]) * t}
        remaining -= length
    return points[-1]


def _point_at(steps: list[dict], target_seconds: float) -> tuple[dict, float]:
    """Return the lat/lng reached after target_seconds of driving, plus the
    elapsed seconds at that point (capped at the route's total).

    Interpolates within the step that crosses target_seconds (along its
    polyline when present) rather than jumping to the step's end, which on a
    long highway step can land far past the requested window.
    """
    elapsed = 0
    for step in steps:
        step_seconds = step["duration"]["value"]
        if step_seconds and elapsed + step_seconds >= target_seconds:
            if "polyline" in step:
                points = googlemaps.convert.decode_polyline(step["polyline"]["points"])
            else:
                points = [step["start_location"], step["end_location"]]
            fraction = (target_seconds - elapsed) / step_seconds
            return _interpolate(points, fraction), target_seconds
        elapsed += step_seconds
    return steps[-1]["end_location"], elapsed


def _sample_points_along_route(steps: list[dict], within_minutes: int) -> list[dict]:
    """Pick up to _ROUTE_SAMPLE_POINTS evenly-spaced points reached within the
    first `within_minutes` of driving, deduplicated by location.
    """
    total_seconds = within_minutes * 60
    points = []
    seen = set()
    for i in range(1, _ROUTE_SAMPLE_POINTS + 1):
        location, elapsed = _point_at(steps, total_seconds * i / _ROUTE_SAMPLE_POINTS)
        key = (round(location["lat"], 4), round(location["lng"], 4))
        if key in seen:
            continue
        seen.add(key)
        points.append({"location": location, "minutes_into_drive": elapsed / 60})
    return points


# Drew's go-to breakfast/food stops. get_nearby_places(kind="food") searches
# all of these by default, so the answer is "which of my brands is close by
# or on the way", not "any restaurant Google ranks highly".
DEFAULT_FOOD_BRANDS = ("Chick-fil-A", "Wawa", "QuikTrip", "Dunkin'")
# Past this a stop isn't worth suggesting. Generous on purpose: Drew will
# sometimes take the Chick-fil-A across town (~18m) anyway.
MAX_DETOUR_SECONDS = 20 * 60


def _resolve_brands(kind: str, keyword: str | None, brands: list[str] | None) -> list[str | None]:
    """The brands to search, in preference order. [None] means any place of
    the kind, with no brand filter."""
    if keyword:
        return [keyword]
    if brands is not None:
        return list(brands) or [None]
    if kind == "food":
        return list(DEFAULT_FOOD_BRANDS)
    return [None]


def _normalize_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _matches_brand(place: dict, brand: str | None) -> bool:
    """Places keyword search is fuzzy (a Dunkin' search can return a pizza
    place), so require the brand in the place's name."""
    return brand is None or _normalize_name(brand) in _normalize_name(place.get("name", ""))


def _meters_between(a: dict, b: dict) -> float:
    """Great-circle distance between two {"lat", "lng"} points."""
    lat1, lng1, lat2, lng2 = map(math.radians, (a["lat"], a["lng"], b["lat"], b["lng"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _search_brands(
    points: list[dict], brands: list[str | None], place_type: str, open_now: bool
) -> list[tuple[float, str | None, dict]]:
    """Search every brand near every point and return unique
    (meters from nearest search point, brand, place) candidates, nearest
    first, so the capped set of detour lookups goes to the likeliest stops.

    Google often returns places outside the requested radius. Those aren't
    dropped here: some are good stops a few miles off the highway, and the
    detour cap is what rules out the wrong-direction ones (a Terre Haute
    QuikTrip for a Bloomington -> Indianapolis route).
    """
    candidates = {}
    for brand in brands:
        for point in points:
            # The brand name is filter enough; a type would drop brands Google
            # files elsewhere (QuikTrip as a gas station, Dunkin' as a cafe).
            search_type = place_type if brand is None else None
            for place in gc.places_nearby(point, brand, search_type, open_now=open_now):
                if not _matches_brand(place, brand):
                    continue
                meters = _meters_between(point, place["geometry"]["location"])
                known = candidates.get(place["place_id"])
                if known is None or meters < known[0]:
                    candidates[place["place_id"]] = (meters, known[1] if known else brand, place)
    return sorted(candidates.values(), key=lambda item: item[0])


def _place_result(place: dict, brand: str | None) -> dict:
    result = {
        "name": place["name"],
        "address": place.get("vicinity"),
        "rating": place.get("rating"),
        "place_id": place["place_id"],
    }
    if brand:
        result["brand"] = brand
    return result


@ttl_cache(ttl_seconds=CACHE_TTL_SECONDS)
def compute_nearby_places(
    kind: str,
    origin: str,
    destination: str | None = None,
    keyword: str | None = None,
    max_results: int = 5,
    within_first_minutes: int | None = None,
    departure_time: str = "now",
    brands: list[str] | None = None,
) -> dict:
    if kind not in _NEARBY_KIND_TO_TYPE:
        raise ValueError("kind must be 'food' or 'gas'")
    if within_first_minutes is not None and not destination:
        raise ValueError("within_first_minutes requires destination")
    place_type = _NEARBY_KIND_TO_TYPE[kind]
    limit = max(1, min(max_results, NEARBY_MAX_RESULTS_CAP))
    search_brands = _resolve_brands(kind, keyword, brands)

    if destination:
        return _compute_nearby_along_route(
            origin, destination, search_brands, place_type, limit, within_first_minutes, departure_time
        )

    departure = gc.resolve_departure_time(departure_time)
    scheduled = _is_scheduled(departure_time)
    origin_result = gc.validate_address(origin)
    origin_location = origin_result.get("geocode", {}).get("location", {})
    start_location = {
        "lat": origin_location.get("latitude"),
        "lng": origin_location.get("longitude"),
    }
    candidates = _search_brands([start_location], search_brands, place_type, open_now=not scheduled)

    scored = []
    for _, brand, place in candidates[: limit * 3]:
        place_location = place["geometry"]["location"]
        leg = gc.fetch_directions(
            origin,
            f"{place_location['lat']},{place_location['lng']}",
            departure_time=departure,
        )["legs"][0]
        result = _place_result(place, brand)
        result["drive_minutes"] = format_duration(leg["duration"]["value"])
        scored.append((leg["duration"]["value"], place, leg["duration"]["value"], result))
    scored.sort(key=lambda item: item[0])

    return {"places": _keep_open([item[1:] for item in scored], departure, scheduled, limit)}


def _compute_nearby_along_route(
    origin: str,
    destination: str,
    brands: list[str | None],
    place_type: str,
    limit: int,
    within_first_minutes: int | None,
    departure_time: str = "now",
) -> dict:
    """Search near points sampled along the route (the first
    within_first_minutes of it, or all of it) and rank stops by how much
    time they add, so the answer is what's actually on the way.
    """
    departure = gc.resolve_departure_time(departure_time)
    scheduled = _is_scheduled(departure_time)
    route = gc.fetch_directions(origin, destination, departure_time=departure)
    leg = route["legs"][0]
    direct_seconds = leg["duration"]["value"]
    window_minutes = within_first_minutes or math.ceil(direct_seconds / 60)
    sample_points = _sample_points_along_route(leg["steps"], window_minutes)

    candidates = _search_brands(
        [sample["location"] for sample in sample_points], brands, place_type, open_now=not scheduled
    )

    window_seconds = window_minutes * 60
    brand_rank = {brand: i for i, brand in enumerate(brands)}
    scored = []
    for _, brand, place in candidates[: limit * 3]:
        to_stop, detour_seconds = _stop_timing(origin, destination, place, departure, direct_seconds)
        if to_stop > window_seconds or detour_seconds > MAX_DETOUR_SECONDS:
            continue
        result = _place_result(place, brand)
        result["detour_minutes"] = format_duration(detour_seconds)
        result["minutes_into_drive"] = format_duration(to_stop)
        # Whole minutes so a 10-second difference doesn't outrank brand preference.
        scored.append(((detour_seconds // 60, brand_rank[brand], to_stop), place, to_stop, result))
    scored.sort(key=lambda item: item[0])

    places = _keep_open([item[1:] for item in scored], departure, scheduled, limit)
    detours = {item[3]["place_id"]: item[0][0] for item in scored}
    return {"summary": _summarize(places, detours), "places": places}


def _summarize(places: list[dict], detour_minutes: dict[str, int]) -> str:
    """One line stating the tradeoff: the stop that adds the least time,
    then how much more each alternative costs on top of it."""
    if not places:
        return "No matching stop on the way."

    def label(place):
        return f"{place.get('brand') or place['name']} ({place['address']})"

    best = places[0]
    best_minutes = detour_minutes[best["place_id"]]
    line = f"{label(best)} adds the least time: {best['detour_minutes']}."
    others = [
        f"{label(p)} adds {p['detour_minutes']} (+{detour_minutes[p['place_id']] - best_minutes}m)"
        for p in places[1:]
    ]
    if others:
        line += " Also: " + "; ".join(others) + "."
    return line


@ttl_cache(ttl_seconds=CACHE_TTL_SECONDS)
def compute_distance_matrix(
    origins: list[str], destinations: list[str], departure_time: str = "now"
) -> dict:
    departure = gc.resolve_departure_time(departure_time)
    result = gc.distance_matrix(origins, destinations, departure)

    matrix = []
    for i, origin in enumerate(origins):
        row = {"origin": origin, "destinations": []}
        for j, destination in enumerate(destinations):
            element = result["rows"][i]["elements"][j]
            row["destinations"].append(
                {
                    "destination": destination,
                    "duration": format_duration(element["duration"]["value"]),
                    "duration_seconds": element["duration"]["value"],
                    "distance": format_distance(element["distance"]["value"]),
                }
            )
        matrix.append(row)

    return {"matrix": matrix, "traffic_adjusted": True}
