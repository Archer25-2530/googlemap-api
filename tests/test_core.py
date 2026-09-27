from datetime import datetime, timedelta, timezone

from maps_mcp import core
from maps_mcp.core import _point_at, _sample_points_along_route


def _step(seconds, start, end):
    return {
        "duration": {"value": seconds},
        "start_location": {"lat": start, "lng": start},
        "end_location": {"lat": end, "lng": end},
    }


STEPS = [
    _step(600, 0.0, 1.0),   # cumulative 600s (10m)
    _step(1200, 1.0, 2.0),  # cumulative 1800s (30m)
    _step(1800, 2.0, 3.0),  # cumulative 3600s (60m)
    _step(600, 3.0, 4.0),   # cumulative 4200s (70m)
]


def test_point_at_interpolates_within_step():
    # 45m is halfway through the 30m-60m step, not its end at 60m.
    location, elapsed = _point_at(STEPS, 2700)
    assert location["lat"] == 2.5
    assert location["lng"] == 2.5
    assert elapsed == 2700


def test_point_at_uses_step_polyline():
    import googlemaps.convert

    path = [{"lat": 0.0, "lng": 0.0}, {"lat": 0.0, "lng": 1.0}, {"lat": 1.0, "lng": 1.0}]
    step = {
        "duration": {"value": 100},
        "start_location": path[0],
        "end_location": path[-1],
        "polyline": {"points": googlemaps.convert.encode_polyline(path)},
    }
    location, _ = _point_at([step], 75)
    assert round(location["lat"], 4) == 0.5
    assert round(location["lng"], 4) == 1.0


def test_point_at_beyond_route_returns_last_point():
    location, elapsed = _point_at(STEPS, 10_000)
    assert location == {"lat": 4.0, "lng": 4.0}
    assert elapsed == 4200


def test_sample_points_along_route_stay_within_window():
    points = _sample_points_along_route(STEPS, within_minutes=45)
    assert [p["minutes_into_drive"] for p in points] == [15, 30, 45]
    assert [p["location"]["lat"] for p in points] == [1.25, 2.0, 2.5]


def test_sample_points_deduplicates_when_route_shorter_than_window():
    # thirds of 200m = 66.7m, 133m, 200m; the last two both clamp to the route's end.
    points = _sample_points_along_route(STEPS, within_minutes=200)
    assert len(points) == 2
    assert points[-1]["location"] == {"lat": 4.0, "lng": 4.0}


def _place(place_id, lat):
    return {"place_id": place_id, "name": place_id, "geometry": {"location": {"lat": lat, "lng": 0}}}


def test_along_route_enforces_window_and_reports_real_detour(monkeypatch):
    direct_seconds = 3660  # 61m direct
    # (seconds from origin to stop, seconds from stop to destination)
    timings = {
        "0.3,0": (25 * 60, 38 * 60),   # Martinsville: 25m in, 2m detour
        "0.5,0": (35 * 60, 31 * 60),   # Mooresville: 35m in, 5m detour
        "0.7,0": (47 * 60, 16 * 60),   # Greenwood: past the 45m window
    }

    def fake_directions(origin, destination, departure_time, waypoints=None):
        if not waypoints:
            return {"legs": [{"duration": {"value": direct_seconds}, "steps": STEPS}]}
        to_stop, from_stop = timings[waypoints[0]]
        return {"legs": [{"duration": {"value": to_stop}}, {"duration": {"value": from_stop}}]}

    monkeypatch.setattr(core.gc, "fetch_directions", fake_directions)
    monkeypatch.setattr(
        core.gc,
        "places_nearby",
        lambda location, keyword, place_type, open_now=True: [_place("mooresville", 0.5), _place("greenwood", 0.7), _place("martinsville", 0.3)],
    )

    places = core._compute_nearby_along_route("A", "B", "Chick-fil-A", "restaurant", 5, 45)["places"]

    assert [p["name"] for p in places] == ["martinsville", "mooresville"]
    assert [p["minutes_into_drive"] for p in places] == ["25m", "35m"]
    assert [p["detour_minutes"] for p in places] == ["2m", "5m"]


EDT = timezone(timedelta(hours=-4))

# Chick-fil-A-style hours: Mon-Sat 06:30-22:00, closed Sunday.
CFA_HOURS = {
    "utc_offset": -240,
    "opening_hours": {
        "periods": [
            {"open": {"day": d, "time": "0630"}, "close": {"day": d, "time": "2200"}}
            for d in range(1, 7)
        ]
    },
}


def test_is_open_at_uses_arrival_time_not_now():
    sunday_night = datetime(2026, 9, 27, 20, 50, tzinfo=EDT)
    monday_morning = datetime(2026, 9, 28, 6, 45, tzinfo=EDT)
    assert core._is_open_at(CFA_HOURS, sunday_night) is False
    assert core._is_open_at(CFA_HOURS, monday_morning) is True
    # Same instant expressed in UTC is evaluated in the place's local time.
    assert core._is_open_at(CFA_HOURS, monday_morning.astimezone(timezone.utc)) is True


def test_is_open_at_handles_overnight_24_7_and_missing_hours():
    overnight = {
        "utc_offset": -240,
        # Saturday 22:00 -> Sunday 02:00 wraps the end of Google's week.
        "opening_hours": {"periods": [{"open": {"day": 6, "time": "2200"}, "close": {"day": 0, "time": "0200"}}]},
    }
    assert core._is_open_at(overnight, datetime(2026, 9, 27, 1, 0, tzinfo=EDT)) is True  # Sun 1am
    assert core._is_open_at(overnight, datetime(2026, 9, 27, 3, 0, tzinfo=EDT)) is False
    always = {"utc_offset": -240, "opening_hours": {"periods": [{"open": {"day": 0, "time": "0000"}}]}}
    assert core._is_open_at(always, datetime(2026, 9, 27, 3, 0, tzinfo=EDT)) is True
    assert core._is_open_at({"utc_offset": -240}, datetime(2026, 9, 27, 3, 0, tzinfo=EDT)) is None


def test_along_route_with_departure_time_checks_hours_at_arrival(monkeypatch):
    def fake_directions(origin, destination, departure_time, waypoints=None):
        if not waypoints:
            return {"legs": [{"duration": {"value": 3660}, "steps": STEPS}]}
        return {"legs": [{"duration": {"value": 25 * 60}}, {"duration": {"value": 38 * 60}}]}

    seen_open_now = []

    def fake_nearby(location, keyword, place_type, open_now=True):
        seen_open_now.append(open_now)
        return [_place("cfa", 0.3), _place("closed-early", 0.31), _place("no-hours", 0.32)]

    hours = {
        "cfa": CFA_HOURS,
        "closed-early": {"utc_offset": -240, "opening_hours": {"periods": [
            {"open": {"day": 1, "time": "1000"}, "close": {"day": 1, "time": "2000"}}
        ]}},
        "no-hours": {},
    }
    monkeypatch.setattr(core.gc, "fetch_directions", fake_directions)
    monkeypatch.setattr(core.gc, "places_nearby", fake_nearby)
    monkeypatch.setattr(core.gc, "place_hours", lambda place_id: hours[place_id])

    places = core._compute_nearby_along_route(
        "A", "B", "Chick-fil-A", "restaurant", 5, 45, "2026-09-28T06:30:00-04:00"
    )["places"]

    assert not any(seen_open_now)  # don't let Places filter on the current time
    assert [p["name"] for p in places] == ["cfa", "no-hours"]
    assert places[0]["arrival_time"] == "2026-09-28T06:55-04:00"
    assert places[0]["open_at_arrival"] is True
    assert places[1]["open_at_arrival"] is None
    assert "open_now" not in places[0]
