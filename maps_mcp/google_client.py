"""Thin wrapper around the Google Maps Directions API."""
import datetime as dt
import os

import googlemaps
import googlemaps.exceptions


class MapsClientError(RuntimeError):
    """Raised when the Google Directions API can't satisfy a request."""


_client: googlemaps.Client | None = None


def get_client() -> googlemaps.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if not api_key:
            raise MapsClientError(
                "GOOGLE_MAPS_API_KEY environment variable is not set"
            )
        _client = googlemaps.Client(key=api_key)
    return _client


def resolve_departure_time(value: str | None) -> dt.datetime:
    """Turn "now"/None or an ISO 8601 string into a timezone-aware datetime."""
    if value is None or value.strip().lower() == "now":
        return dt.datetime.now().astimezone()
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"departure_time must be 'now' or an ISO 8601 timestamp, got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def fetch_directions(
    origin: str,
    destination: str,
    departure_time: dt.datetime,
    waypoints: list[str] | None = None,
) -> dict:
    """Call the Directions API and return the first route, legs included."""
    client = get_client()
    try:
        routes = client.directions(
            origin,
            destination,
            mode="driving",
            waypoints=waypoints or None,
            optimize_waypoints=False,
            departure_time=departure_time,
            traffic_model="best_guess",
        )
    except googlemaps.exceptions.ApiError as exc:
        raise MapsClientError(f"Google Directions API error: {exc}") from exc
    except googlemaps.exceptions.TransportError as exc:
        raise MapsClientError(f"Failed to reach Google Directions API: {exc}") from exc

    if not routes:
        raise MapsClientError(f"No route found between {origin!r} and {destination!r}")
    return routes[0]


def validate_address(address: str) -> dict:
    """Validate/normalize a free-form address via the Address Validation API."""
    client = get_client()
    try:
        response = client.addressvalidation([address], enableUspsCass=False)
    except googlemaps.exceptions.ApiError as exc:
        raise MapsClientError(f"Google Address Validation API error: {exc}") from exc
    except googlemaps.exceptions.TransportError as exc:
        raise MapsClientError(
            f"Failed to reach Google Address Validation API: {exc}"
        ) from exc

    result = response.get("result")
    if not result:
        raise MapsClientError(f"Address not found: {address!r}")
    return result


def places_nearby(location: dict, keyword: str, place_type: str, radius: int = 5000) -> list[dict]:
    """Search for open places of a given type/keyword near a lat/lng."""
    client = get_client()
    try:
        response = client.places_nearby(
            location=location,
            radius=radius,
            keyword=keyword,
            type=place_type,
            open_now=True,
        )
    except googlemaps.exceptions.ApiError as exc:
        raise MapsClientError(f"Google Places API error: {exc}") from exc
    except googlemaps.exceptions.TransportError as exc:
        raise MapsClientError(f"Failed to reach Google Places API: {exc}") from exc

    return response.get("results", [])


def distance_matrix(
    origins: list[str],
    destinations: list[str],
    departure_time: dt.datetime,
) -> dict:
    """Call the Distance Matrix API for a grid of origins x destinations."""
    client = get_client()
    try:
        return client.distance_matrix(
            origins=origins,
            destinations=destinations,
            mode="driving",
            departure_time=departure_time,
            traffic_model="best_guess",
        )
    except googlemaps.exceptions.ApiError as exc:
        raise MapsClientError(f"Google Distance Matrix API error: {exc}") from exc
    except googlemaps.exceptions.TransportError as exc:
        raise MapsClientError(f"Failed to reach Google Distance Matrix API: {exc}") from exc
