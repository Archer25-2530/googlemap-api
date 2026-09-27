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


# Sample points for a 45-minute window over STEPS sit at lat = lng = 1.25, 2.0, 2.5.
NEAR_15M, NEAR_30M, NEAR_45M = 1.25, 2.0, 2.5


def _place(place_id, name, at):
    return {
        "place_id": place_id,
        "name": name,
        "vicinity": f"{place_id} St",
        "geometry": {"location": {"lat": at, "lng": at}},
    }


def _fake_route(monkeypatch, timings, direct_seconds=3660):
    """timings: place lat -> (seconds origin->stop, seconds stop->destination)."""

    def fake_directions(origin, destination, departure_time, waypoints=None):
        if not waypoints:
            return {"legs": [{"duration": {"value": direct_seconds}, "steps": STEPS}]}
        to_stop, from_stop = timings[float(waypoints[0].split(",")[0])]
        return {"legs": [{"duration": {"value": to_stop}}, {"duration": {"value": from_stop}}]}

    monkeypatch.setattr(core.gc, "fetch_directions", fake_directions)


def _fake_places(monkeypatch, by_brand, calls=None):
    def fake_nearby(location, keyword, place_type, open_now=True):
        if calls is not None:
            calls.append((keyword, place_type, open_now))
        return by_brand.get(keyword, [])

    monkeypatch.setattr(core.gc, "places_nearby", fake_nearby)


def test_default_food_search_covers_all_brands_ranked_by_detour(monkeypatch):
    _fake_route(
        monkeypatch,
        {
            NEAR_15M: (16 * 60, 63 * 60),  # CFA across town: 16m in, 18m detour
            NEAR_30M: (26 * 60, 41 * 60),  # Dunkin' on the way: 26m in, 6m detour
        },
    )
    calls = []
    _fake_places(
        monkeypatch,
        {
            "Chick-fil-A": [_place("cfa", "Chick-fil-A", NEAR_15M)],
            "Dunkin'": [_place("dunkin", "Dunkin'", NEAR_30M)],
        },
        calls,
    )

    result = core.compute_nearby_places.__wrapped__(
        "food", "A", "B", within_first_minutes=45
    )

    assert {keyword for keyword, _, _ in calls} == set(core.DEFAULT_FOOD_BRANDS)
    assert all(place_type is None for _, place_type, _ in calls)  # brand search, no type filter
    assert [(p["brand"], p["detour_minutes"], p["minutes_into_drive"]) for p in result["places"]] == [
        ("Dunkin'", "6m", "26m"),
        ("Chick-fil-A", "18m", "16m"),
    ]
    assert result["summary"] == (
        "Dunkin' (dunkin St) adds the least time: 6m. Also: Chick-fil-A (cfa St) adds 18m (+12m)."
    )


def test_along_route_drops_wrong_direction_wrong_brand_and_over_cap(monkeypatch):
    _fake_route(
        monkeypatch,
        {
            NEAR_15M: (15 * 60, 67 * 60),  # 21m detour: over the 20m cap
            NEAR_45M: (62 * 60, 1 * 60),  # past the 60m window
            NEAR_30M: (25 * 60, 38 * 60),  # 2m detour: kept
            9.0: (59 * 60, 70 * 60),  # Terre Haute: 1h 8m detour
        },
    )
    _fake_places(
        monkeypatch,
        {
            "QuikTrip": [
                _place("terre-haute", "QuikTrip", 9.0),  # returned by Google, wrong direction
                _place("too-far", "QuikTrip Store #1", NEAR_15M),
                _place("late", "QuikTrip", NEAR_45M),
                _place("ok", "QuikTrip Store #2", NEAR_30M),
            ],
            "Dunkin'": [_place("pizza", "Hunt Brothers Pizza", NEAR_30M)],
        },
    )

    places = core._compute_nearby_along_route(
        "A", "B", ["QuikTrip", "Dunkin'"], "restaurant", 5, 60
    )["places"]

    assert [p["place_id"] for p in places] == ["ok"]
    assert places[0]["detour_minutes"] == "2m"


def test_equal_detours_fall_back_to_brand_preference(monkeypatch):
    _fake_route(monkeypatch, {NEAR_30M: (25 * 60, 38 * 60), NEAR_45M: (35 * 60, 28 * 60 + 20)})
    _fake_places(
        monkeypatch,
        {
            "Dunkin'": [_place("dunkin", "Dunkin'", NEAR_30M)],
            "Chick-fil-A": [_place("cfa", "Chick-fil-A", NEAR_45M)],
        },
    )

    places = core._compute_nearby_along_route(
        "A", "B", ["Chick-fil-A", "Dunkin'"], "restaurant", 5, 45
    )["places"]

    # 2m vs 2m20s: same whole minute, so the preferred brand wins.
    assert [p["brand"] for p in places] == ["Chick-fil-A", "Dunkin'"]


def test_resolve_brands():
    assert core._resolve_brands("food", None, None) == list(core.DEFAULT_FOOD_BRANDS)
    assert core._resolve_brands("food", "Chipotle", None) == ["Chipotle"]
    assert core._resolve_brands("food", None, ["Wawa"]) == ["Wawa"]
    assert core._resolve_brands("food", None, []) == [None]
    assert core._resolve_brands("gas", None, None) == [None]


def test_along_route_with_departure_time_checks_hours_at_arrival(monkeypatch):
    _fake_route(monkeypatch, {NEAR_15M: (25 * 60, 38 * 60), NEAR_30M: (26 * 60, 38 * 60), NEAR_45M: (27 * 60, 38 * 60)})
    calls = []
    _fake_places(
        monkeypatch,
        {
            "Chick-fil-A": [
                _place("cfa", "Chick-fil-A", NEAR_15M),
                _place("closed-early", "Chick-fil-A", NEAR_30M),
                _place("no-hours", "Chick-fil-A", NEAR_45M),
            ]
        },
        calls,
    )
    hours = {
        "cfa": CFA_HOURS,
        "closed-early": {"utc_offset": -240, "opening_hours": {"periods": [
            {"open": {"day": 1, "time": "1000"}, "close": {"day": 1, "time": "2000"}}
        ]}},
        "no-hours": {},
    }
    monkeypatch.setattr(core.gc, "place_hours", lambda place_id: hours[place_id])

    places = core._compute_nearby_along_route(
        "A", "B", ["Chick-fil-A"], "restaurant", 5, 45, "2026-09-28T06:30:00-04:00"
    )["places"]

    assert not any(open_now for _, _, open_now in calls)  # don't let Places filter on the current time
    assert [p["place_id"] for p in places] == ["cfa", "no-hours"]
    assert places[0]["arrival_time"] == "2026-09-28T06:55-04:00"
    assert places[0]["open_at_arrival"] is True
    assert places[1]["open_at_arrival"] is None
    assert "open_now" not in places[0]


def test_crowded_last_point_cannot_starve_an_on_the_way_stop(monkeypatch):
    # Live failure: with a planned departure Google returns every QuikTrip in
    # south Indianapolis near the 45m point. They were all nearer their point
    # than the Martinsville Dunkin' was to its own, took all 15 detour-lookup
    # slots, were all past the window, and the result came back empty.
    quiktrips = [_place(f"qt{i}", "QuikTrip", NEAR_45M + i * 0.0002) for i in range(15)]
    timings = {q["geometry"]["location"]["lat"]: (50 * 60, 12 * 60) for q in quiktrips}
    timings[NEAR_30M + 0.01] = (26 * 60, 41 * 60)  # Dunkin' ~1 km off the 30m point
    _fake_route(monkeypatch, timings)
    _fake_places(
        monkeypatch,
        {"QuikTrip": quiktrips, "Dunkin'": [_place("dunkin", "Dunkin'", NEAR_30M + 0.01)]},
    )

    places = core._compute_nearby_along_route(
        "A", "B", ["QuikTrip", "Dunkin'"], "restaurant", 5, 45
    )["places"]

    assert [p["place_id"] for p in places] == ["dunkin"]
    assert places[0]["detour_minutes"] == "6m"
