"""MCP tool definitions for maps-mcp."""
import hmac
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from .core import (
    compute_distance_matrix,
    compute_drive_time,
    compute_geocode,
    compute_places,
    compute_route_eta,
)
from .google_client import MapsClientError
from .mcp_app import mcp


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
