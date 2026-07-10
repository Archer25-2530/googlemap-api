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
    origin: str, destination: str, keyword: str, place_type: str = "restaurant"
) -> dict:
    departure = gc.resolve_departure_time("now")
    route = gc.fetch_directions(origin, destination, departure_time=departure)
    start_location = route["legs"][0]["start_location"]
    raw_places = gc.places_nearby(start_location, keyword, place_type)

    places = []
    for place in raw_places[:3]:
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
