from maps_mcp.formatting import format_distance, format_duration


def test_format_duration_hours_and_minutes():
    assert format_duration(10020) == "2h 47m"


def test_format_duration_minutes_only():
    assert format_duration(720) == "12m"


def test_format_duration_zero():
    assert format_duration(0) == "0m"


def test_format_distance_rounds_to_whole_miles():
    assert format_distance(300_000) == "186 mi"
