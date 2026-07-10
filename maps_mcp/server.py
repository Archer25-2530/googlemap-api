"""MCP tool definitions for maps-mcp."""
from .core import compute_drive_time, compute_route_eta
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
