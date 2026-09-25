from maps_mcp.core import _point_after, _sample_points_along_route


def _step(seconds, lat, lng):
    return {"duration": {"value": seconds}, "end_location": {"lat": lat, "lng": lng}}


STEPS = [
    _step(600, 1.0, 1.0),   # cumulative 600s (10m)
    _step(1200, 2.0, 2.0),  # cumulative 1800s (30m)
    _step(1800, 3.0, 3.0),  # cumulative 3600s (60m)
    _step(600, 4.0, 4.0),   # cumulative 4200s (70m)
]


def test_point_after_returns_first_step_reaching_target():
    location, elapsed = _point_after(STEPS, 1500)
    assert location == {"lat": 2.0, "lng": 2.0}
    assert elapsed == 1800


def test_point_after_beyond_route_returns_last_point():
    location, elapsed = _point_after(STEPS, 10_000)
    assert location == {"lat": 4.0, "lng": 4.0}
    assert elapsed == 4200


def test_sample_points_along_route_within_45_minutes():
    points = _sample_points_along_route(STEPS, within_minutes=45)
    # thirds of 45m = 15m, 30m, 45m -> steps reaching 1800s, 1800s (dedup), 3600s
    assert [p["location"] for p in points] == [
        {"lat": 2.0, "lng": 2.0},
        {"lat": 3.0, "lng": 3.0},
    ]
    assert points[0]["minutes_into_drive"] == 30
    assert points[1]["minutes_into_drive"] == 60


def test_sample_points_deduplicates_when_route_shorter_than_window():
    points = _sample_points_along_route(STEPS, within_minutes=200)
    assert len(points) == 1
    assert points[0]["location"] == {"lat": 4.0, "lng": 4.0}
