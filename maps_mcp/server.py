"""MCP tool definitions for maps-mcp."""
import hmac
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from .core import compute_drive_time, compute_route_eta
from .google_client import MapsClientError
from .mcp_app import mcp


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


# GET-only HTTP endpoint for clients that can't do OAuth/MCP (e.g. tinyNature
# executors, which only issue GET requests). Gated by a dedicated shared
# secret (TINYNATURE_API_KEY) that is unrelated to GOOGLE_MAPS_API_KEY, so a
# leaked query-string key never exposes the real (billable) Google key.
@mcp.custom_route("/api/drive-time", methods=["GET"])
async def api_drive_time(request: Request) -> JSONResponse:
    expected_key = os.environ.get("TINYNATURE_API_KEY", "")
    provided_key = request.query_params.get("key", "")
    if not expected_key or not hmac.compare_digest(provided_key, expected_key):
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
