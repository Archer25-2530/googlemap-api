"""Business logic behind the two MCP tools. Kept free of any MCP/transport
concerns so it can be unit tested and reused by scripts/validate_routes.py.
"""
from datetime import timedelta

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
) -> dict:
    departure = gc.resolve_departure_time("now")
    route = gc.fetch_directions(origin, destination, departure_time=departure)
    start_location = route["legs"][0]["start_location"]
    raw_places = gc.places_nearby(start_location, keyword, place_type)

    places = []
    for place in raw_places[:max_results]:
        place_location = place["geometry"]["location"]
        detour_route = gc.fetch_directions(
            origin,
            f"{place_location['lat']},{place_location['lng']}",
            departure_time=departure,
        )
        detour_leg = detour_route["legs"][0]
        places.append(
            {
                "name": place["name"],
                "address": place.get("vicinity"),
                "rating": place.get("rating"),
                "open_now": place.get("opening_hours", {}).get("open_now"),
                "detour_minutes": format_duration(detour_leg["duration"]["value"]),
                "place_id": place["place_id"],
            }
        )

    return {
        "origin": origin,
        "destination": destination,
        "keyword": keyword,
        "type": place_type,
        "places": places,
    }


_NEARBY_KIND_TO_TYPE = {"food": "restaurant", "gas": "gas_station"}
NEARBY_MAX_RESULTS_CAP = 8
_ROUTE_SAMPLE_POINTS = 3


def _point_after(steps: list[dict], target_seconds: float) -> tuple[dict, float]:
    """Walk a route's steps and return the lat/lng reached at or after
    target_seconds of cumulative driving time, plus the actual elapsed seconds.
    """
    elapsed = 0
    for step in steps:
        elapsed += step["duration"]["value"]
        if elapsed >= target_seconds:
            return step["end_location"], elapsed
    return steps[-1]["end_location"], elapsed


def _sample_points_along_route(steps: list[dict], within_minutes: int) -> list[dict]:
    """Pick up to _ROUTE_SAMPLE_POINTS evenly-spaced points reached within the
    first `within_minutes` of driving, deduplicated by location.
    """
    total_seconds = within_minutes * 60
    points = []
    seen = set()
    for i in range(1, _ROUTE_SAMPLE_POINTS + 1):
        location, elapsed = _point_after(steps, total_seconds * i / _ROUTE_SAMPLE_POINTS)
        key = (round(location["lat"], 4), round(location["lng"], 4))
        if key in seen:
            continue
        seen.add(key)
        points.append({"location": location, "minutes_into_drive": elapsed / 60})
    return points


@ttl_cache(ttl_seconds=CACHE_TTL_SECONDS)
def compute_nearby_places(
    kind: str,
    origin: str,
    destination: str | None = None,
    keyword: str | None = None,
    max_results: int = 5,
    within_first_minutes: int | None = None,
) -> dict:
    if kind not in _NEARBY_KIND_TO_TYPE:
        raise ValueError("kind must be 'food' or 'gas'")
    if within_first_minutes is not None and not destination:
        raise ValueError("within_first_minutes requires destination")
    place_type = _NEARBY_KIND_TO_TYPE[kind]
    limit = max(1, min(max_results, NEARBY_MAX_RESULTS_CAP))

    if within_first_minutes:
        return _compute_nearby_along_route(
            origin, destination, keyword, place_type, limit, within_first_minutes
        )

    if destination:
        result = compute_places(origin, destination, keyword, place_type, max_results=limit)
        return {"places": result["places"]}

    departure = gc.resolve_departure_time("now")
    origin_result = gc.validate_address(origin)
    origin_location = origin_result.get("geocode", {}).get("location", {})
    start_location = {
        "lat": origin_location.get("latitude"),
        "lng": origin_location.get("longitude"),
    }
    raw_places = gc.places_nearby(start_location, keyword, place_type)

    scored = []
    for place in raw_places[:limit]:
        place_location = place["geometry"]["location"]
        leg = gc.fetch_directions(
            origin,
            f"{place_location['lat']},{place_location['lng']}",
            departure_time=departure,
        )["legs"][0]
        scored.append(
            (
                leg["distance"]["value"],
                {
                    "name": place["name"],
                    "address": place.get("vicinity"),
                    "rating": place.get("rating"),
                    "open_now": place.get("opening_hours", {}).get("open_now"),
                    "place_id": place["place_id"],
                },
            )
        )
    scored.sort(key=lambda item: item[0])

    return {"places": [place for _, place in scored]}


def _compute_nearby_along_route(
    origin: str,
    destination: str,
    keyword: str | None,
    place_type: str,
    limit: int,
    within_first_minutes: int,
) -> dict:
    """Search near points sampled along the route, truncated to the first
    within_first_minutes of driving, so results are actually on the way
    rather than just near the origin or ranked by detour off the full route.
    """
    departure = gc.resolve_departure_time("now")
    route = gc.fetch_directions(origin, destination, departure_time=departure)
    steps = route["legs"][0]["steps"]
    sample_points = _sample_points_along_route(steps, within_first_minutes)

    seen_place_ids = set()
    candidates = []
    for sample in sample_points:
        for place in gc.places_nearby(sample["location"], keyword, place_type):
            if place["place_id"] in seen_place_ids:
                continue
            seen_place_ids.add(place["place_id"])
            candidates.append(place)

    scored = []
    for place in candidates[: limit * 3]:
        place_location = place["geometry"]["location"]
        leg = gc.fetch_directions(
            origin,
            f"{place_location['lat']},{place_location['lng']}",
            departure_time=departure,
        )["legs"][0]
        duration_seconds = leg["duration"]["value"]
        scored.append(
            (
                duration_seconds,
                {
                    "name": place["name"],
                    "address": place.get("vicinity"),
                    "rating": place.get("rating"),
                    "open_now": place.get("opening_hours", {}).get("open_now"),
                    "detour_minutes": format_duration(duration_seconds),
                    "minutes_into_drive": format_duration(duration_seconds),
                    "place_id": place["place_id"],
                },
            )
        )
    scored.sort(key=lambda item: item[0])

    return {"places": [place for _, place in scored[:limit]]}


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
