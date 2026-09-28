"""MCP tool definitions for maps-mcp."""
import hmac
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from .core import (
    compute_distance_matrix,
    compute_drive_time,
    compute_geocode,
    compute_nearby_places,
    compute_places,
    compute_route_eta,
)
from .google_client import MapsClientError
from .mcp_app import mcp
from .weather import WeatherError, compute_weather


def _authorized(request: Request) -> bool:
    """Constant-time check of the shared TINYNATURE_API_KEY query param.

    Dedicated secret, unrelated to GOOGLE_MAPS_API_KEY, so a leaked
    query-string key never exposes the real (billable) Google key.
    """
    expected_key = os.environ.get("TINYNATURE_API_KEY", "")
    provided_key = request.query_params.get("key", "")
    return bool(expected_key) and hmac.compare_digest(provided_key, expected_key)


@mcp.tool
def get_drive_time(origin: str, destination: str, departure_time: str = "now") -> dict:
    """Get real drive time and distance between two addresses, traffic-adjusted.

    Use for single-leg travel blocks (e.g. home -> site visit).

    Args:
        origin: Starting address.
        destination: Ending address.
        departure_time: "now" (default) or an ISO 8601 timestamp for a future departure.
    """
    return compute_drive_time(origin, destination, departure_time)


@mcp.tool
def get_route_eta(waypoints: list[str], departure_time: str = "now") -> dict:
    """Get total drive time, distance, arrival time, and a per-leg breakdown
    for an ordered multi-stop route.

    Use for multi-stop trips (e.g. home -> daycare -> site visit).

    Args:
        waypoints: Ordered list of addresses, first is the origin, last is the destination.
        departure_time: "now" (default) or an ISO 8601 timestamp for a future departure.
    """
    return compute_route_eta(waypoints, departure_time)


@mcp.tool
def get_nearby_places(
    kind: str,
    origin: str,
    destination: str | None = None,
    keyword: str | None = None,
    max_results: int = 5,
    within_first_minutes: int | None = None,
    departure_time: str = "now",
    brands: list[str] | None = None,
) -> dict:
    """Find food or gas stops from brands Drew likes that are close by or on the way.

    Use for breakfast/lunch/fuel on work trips. kind="food" searches Chick-fil-A,
    Wawa, QuikTrip and Dunkin' by default. With a destination, results are sorted
    by how much time the stop adds (smallest first, ties broken in that brand
    order), and stops that add more than 15 minutes or aren't near the route are
    left out. An empty list means none of the brands is on the way.

    Args:
        kind: "food" or "gas".
        origin: Address or "lat,lng" to start from.
        destination: Optional address. If given, searches along the route. Without it,
            only stops within 25 miles (straight line) of origin are returned.
        keyword: Optional single brand, e.g. "Chick-fil-A"; overrides brands.
        max_results: Number of results to return (default 5, max 8).
        within_first_minutes: Optional; requires destination. Only returns stops reached
            within this many minutes of driving from origin, e.g. 45 for breakfast.
            Without it, the whole route is searched.
        departure_time: "now" (default) returns places open right now, with open_now.
            An ISO 8601 timestamp for a planned departure (e.g. "2026-09-28T06:00:00-04:00")
            instead returns places open when you'd arrive at them, with arrival_time and
            open_at_arrival (null if Google has no hours for the place).
        brands: Optional list of brands to search instead of the default, in preference
            order. [] searches any restaurant / gas station with no brand filter.

    Each result has brand, detour_minutes (time added over the direct route) and
    minutes_into_drive when a destination is given, or drive_minutes without one.
    """
    return compute_nearby_places(
        kind, origin, destination, keyword, max_results, within_first_minutes, departure_time, brands
    )


@mcp.tool
def get_weather(
    location: str,
    start_date: str,
    end_date: str | None = None,
    hourly_at: str | None = None,
) -> dict:
    """Get a daily weather forecast for a location and date range.

    Days within Google's 10-day forecast window use real forecasts; days
    beyond that use a 10-year historical average, marked "typical" in the
    output. Never fills in numbers on failure — check for an "error" key.

    Args:
        location: Free-text address/city, or "lat,lng".
        start_date: First day to forecast, "YYYY-MM-DD" local to the location.
        end_date: Last day to forecast, "YYYY-MM-DD" (default: start_date). Max 21-day span.
        hourly_at: Optional "HH:MM" local time, e.g. a site's morning start time;
            adds temp_at_hour_f to each forecast day (Google for the next 24 hours,
            Open-Meteo for later forecast days; see temp_at_hour_source).
    """
    try:
        return compute_weather(location, start_date, end_date, hourly_at)
    except WeatherError as exc:
        return {"error": str(exc)}


# GET-only HTTP endpoints below, for clients that can't do OAuth/MCP (e.g.
# tinyNature executors, which only issue GET requests). Each is gated by
# _authorized() above.
@mcp.custom_route("/api/drive-time", methods=["GET"])
async def api_drive_time(request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    origin = request.query_params.get("origin")
    destination = request.query_params.get("destination")
    if not origin or not destination:
        return JSONResponse(
            {"error": "origin and destination query params are required"},
            status_code=400,
        )
    departure_time = request.query_params.get("departure_time", "now")

    try:
        result = compute_drive_time(origin, destination, departure_time)
    except MapsClientError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse(result)


@mcp.custom_route("/api/geocode", methods=["GET"])
async def api_geocode(request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    address = request.query_params.get("address")
    if not address:
        return JSONResponse(
            {"error": "address query param is required"}, status_code=400
        )

    try:
        result = compute_geocode(address)
    except MapsClientError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    return JSONResponse(result)


@mcp.custom_route("/api/places", methods=["GET"])
async def api_places(request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    origin = request.query_params.get("origin")
    destination = request.query_params.get("destination")
    keyword = request.query_params.get("keyword")
    if not origin or not destination or not keyword:
        return JSONResponse(
            {"error": "origin, destination, and keyword query params are required"},
            status_code=400,
        )
    place_type = request.query_params.get("type", "restaurant")

    try:
        result = compute_places(origin, destination, keyword, place_type)
    except MapsClientError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    return JSONResponse(result)


@mcp.custom_route("/api/distance-matrix", methods=["GET"])
async def api_distance_matrix(request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    origins_param = request.query_params.get("origins")
    destinations_param = request.query_params.get("destinations")
    if not origins_param or not destinations_param:
        return JSONResponse(
            {"error": "origins and destinations query params are required"},
            status_code=400,
        )
    origins = [o.strip() for o in origins_param.split("|") if o.strip()]
    destinations = [d.strip() for d in destinations_param.split("|") if d.strip()]

    try:
        result = compute_distance_matrix(origins, destinations)
    except MapsClientError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    return JSONResponse(result)
