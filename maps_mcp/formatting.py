"""Human-readable formatting helpers for durations and distances."""

METERS_PER_MILE = 1609.344


def format_duration(seconds: int) -> str:
    """Format a duration in seconds as "Xh Ym" (or "Ym" under an hour)."""
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_distance(meters: float) -> str:
    """Format a distance in meters as whole miles, e.g. "187 mi"."""
    miles = meters / METERS_PER_MILE
    return f"{round(miles)} mi"
