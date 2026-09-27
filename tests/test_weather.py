import datetime as dt

import pytest

from maps_mcp import weather


@pytest.fixture(autouse=True)
def reset_budget():
    weather._google_call_budget["date"] = None
    weather._google_call_budget["count"] = 0
    yield


def _daily_response():
    return {
        "timeZone": {"id": "America/Indiana/Indianapolis"},
        "forecastDays": [
            {
                "displayDate": {"year": 2026, "month": 10, "day": 1},
                "maxTemperature": {"degrees": 72.4},
                "minTemperature": {"degrees": 51.2},
                "daytimeForecast": {
                    "precipitation": {"probability": {"percent": 20}},
                    "weatherCondition": {"description": {"text": "Mostly sunny"}},
                },
                "nighttimeForecast": {
                    "precipitation": {"probability": {"percent": 40}},
                },
            }
        ],
    }


def test_extract_daily_entries_uses_higher_of_day_night_precip():
    entries = weather._extract_daily_entries(_daily_response())
    entry = entries["2026-10-01"]
    assert entry["high_f"] == 72
    assert entry["low_f"] == 51
    assert entry["precip_chance_pct"] == 40
    assert entry["summary"] == "Mostly sunny"
    assert entry["source"] == "forecast"


def test_extract_daily_entries_reads_snow_from_matching_precip_type():
    response = _daily_response()
    response["forecastDays"][0]["daytimeForecast"]["precipitation"]["precipitationType"] = "SNOW"
    response["forecastDays"][0]["daytimeForecast"]["precipitation"]["qpf"] = {"quantity": 1.5}
    entries = weather._extract_daily_entries(response)
    assert entries["2026-10-01"]["snow_in"] == 1.5


def test_extract_hour_temp_picks_closest_interval():
    hourly_response = {
        "forecastHours": [
            {"interval": {"startTime": "2026-10-01T13:00:00Z"}, "temperature": {"degrees": 60}},
            {"interval": {"startTime": "2026-10-01T14:00:00Z"}, "temperature": {"degrees": 65}},
        ]
    }
    # 09:00 local America/Indiana/Indianapolis (UTC-4 in Oct) == 13:00 UTC
    temp = weather._extract_hour_temp(
        hourly_response, "2026-10-01", "09:00", "America/Indiana/Indianapolis"
    )
    assert temp == 60


def test_extract_hour_temp_returns_none_when_hour_is_past_the_page():
    # Google caps a page at 24 hours; the last entry here is 21:00 local on
    # 09-27, and the caller wants 09:00 on 09-28. Nearest-entry used to
    # return the 21:00 reading as if it were the 09:00 one.
    hourly_response = {
        "forecastHours": [
            {"interval": {"startTime": "2026-09-28T00:00:00Z"}, "temperature": {"degrees": 61}},
        ]
    }
    temp = weather._extract_hour_temp(
        hourly_response, "2026-09-28", "09:00", "America/Indiana/Indianapolis"
    )
    assert temp is None


def test_resolve_location_parses_lat_lng_without_geocoding(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not geocode a lat,lng location")

    monkeypatch.setattr(weather.gc, "validate_address", fail_if_called)
    lat, lng, label = weather._resolve_location("39.17,-86.53")
    assert (lat, lng) == (39.17, -86.53)
    assert label == "39.17,-86.53"


def test_resolve_location_raises_weather_error_on_geocode_failure(monkeypatch):
    def raise_maps_error(address):
        raise weather.gc.MapsClientError("not found")

    monkeypatch.setattr(weather.gc, "validate_address", raise_maps_error)
    with pytest.raises(weather.WeatherError, match="geocode_failed"):
        weather._resolve_location("asdfqwer")


def test_google_call_budget_raises_once_cap_reached(monkeypatch):
    monkeypatch.setattr(weather, "WEATHER_DAILY_CAP", 2)
    weather._reserve_google_call_budget()
    weather._reserve_google_call_budget()
    with pytest.raises(weather.WeatherError, match="daily_weather_cap_reached"):
        weather._reserve_google_call_budget()


def test_google_call_budget_resets_on_new_utc_day(monkeypatch):
    monkeypatch.setattr(weather, "WEATHER_DAILY_CAP", 1)
    weather._reserve_google_call_budget()
    with pytest.raises(weather.WeatherError):
        weather._reserve_google_call_budget()

    weather._google_call_budget["date"] = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    weather._reserve_google_call_budget()  # does not raise: new day, budget reset


def test_fetch_typical_days_averages_same_calendar_date_across_years(monkeypatch):
    class FakeResponse:
        ok = True

        def json(self):
            return {
                "daily": {
                    "time": ["2016-10-30", "2017-10-30", "2018-10-30"],
                    "temperature_2m_max": [80.0, 90.0, 100.0],
                    "temperature_2m_min": [60.0, 70.0, 80.0],
                    "precipitation_sum": [0.0, 0.1, 0.02],
                    "snowfall_sum": [0.0, 0.0, 0.0],
                }
            }

    def fake_get(url, params=None, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(weather.requests, "get", fake_get)
    results = weather._fetch_typical_days(22.89, -109.91, ["2026-10-30"])
    entry = results["2026-10-30"]
    assert entry["high_f"] == 90
    assert entry["low_f"] == 70
    assert entry["precip_chance_pct"] == 33  # 1 of 3 years >= 0.04in
    assert entry["source"] == "typical"


def test_google_weather_request_retries_once_on_5xx_then_raises(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    calls = {"count": 0}

    class FakeResponse:
        status_code = 500
        ok = False

    def fake_get(url, params=None, timeout=None):
        calls["count"] += 1
        return FakeResponse()

    monkeypatch.setattr(weather.requests, "get", fake_get)
    with pytest.raises(weather.WeatherError, match="weather_request_failed"):
        weather._google_weather_request("https://example.invalid", {})
    assert calls["count"] == 2


def test_google_weather_request_does_not_retry_on_4xx(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    calls = {"count": 0}

    class FakeResponse:
        status_code = 400
        ok = False

    def fake_get(url, params=None, timeout=None):
        calls["count"] += 1
        return FakeResponse()

    monkeypatch.setattr(weather.requests, "get", fake_get)
    with pytest.raises(weather.WeatherError, match="weather_request_failed"):
        weather._google_weather_request("https://example.invalid", {})
    assert calls["count"] == 1


def test_compute_weather_falls_back_to_typical_when_google_omits_a_date(monkeypatch):
    # Google's forecast window is anchored to local time, so it can legitimately
    # return fewer days than our UTC-based cutoff assumed. That date must still
    # show up, backed by typical data, not silently disappear.
    today = weather._today_utc()
    d0 = today.isoformat()
    d1 = (today + dt.timedelta(days=1)).isoformat()

    def fake_daily(lat, lng):
        return {
            "timeZone": {"id": "America/Indiana/Indianapolis"},
            "forecastDays": [
                {
                    "displayDate": {"year": today.year, "month": today.month, "day": today.day},
                    "maxTemperature": {"degrees": 70},
                    "minTemperature": {"degrees": 50},
                    "daytimeForecast": {"precipitation": {"probability": {"percent": 10}}},
                    "nighttimeForecast": {"precipitation": {"probability": {"percent": 10}}},
                }
                # d1 deliberately missing, unlike what the date-arithmetic cutoff assumed
            ],
        }

    def fake_typical(lat, lng, dates):
        return {d: {"date": d, "high_f": 65, "low_f": 45, "precip_chance_pct": 5, "snow_in": 0.0,
                     "summary": "typical", "source": "typical"} for d in dates}

    def fail_resolve(location):
        return 39.17, -86.53, location

    monkeypatch.setattr(weather, "_fetch_daily_forecast", fake_daily)
    monkeypatch.setattr(weather, "_fetch_typical_days", fake_typical)
    monkeypatch.setattr(weather, "_resolve_location", fail_resolve)

    result = weather.compute_weather("Indianapolis, IN", d0, d1)
    by_date = {day["date"]: day for day in result["days"]}
    assert d0 in by_date and by_date[d0]["source"] == "forecast"
    assert d1 in by_date and by_date[d1]["source"] == "typical"


def test_compute_weather_requests_enough_hours_to_include_exact_boundary(monkeypatch):
    # Regression for the Indianapolis "9:00 reads like the overnight low" bug:
    # when now and the target hour are an exact integer number of hours apart,
    # ceil() alone under-requests by one bucket, so the target hour's own
    # forecastHours entry never gets fetched and a nearer (colder) hour wins.
    today = weather._today_utc()
    d0 = today.isoformat()
    fixed_now = dt.datetime.combine(today, dt.time(5, 0), tzinfo=dt.timezone.utc)

    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(weather.dt, "datetime", FixedDateTime)

    def fake_daily(lat, lng):
        return {
            "timeZone": {"id": "UTC"},
            "forecastDays": [
                {
                    "displayDate": {"year": today.year, "month": today.month, "day": today.day},
                    "maxTemperature": {"degrees": 70},
                    "minTemperature": {"degrees": 46},
                    "daytimeForecast": {"precipitation": {"probability": {"percent": 10}}},
                    "nighttimeForecast": {"precipitation": {"probability": {"percent": 10}}},
                }
            ],
        }

    captured = {}

    def fake_hourly(lat, lng, hours):
        captured["hours"] = hours
        return {"forecastHours": []}

    monkeypatch.setattr(weather, "_fetch_daily_forecast", fake_daily)
    monkeypatch.setattr(weather, "_fetch_hourly_forecast", fake_hourly)
    monkeypatch.setattr(weather, "_resolve_location", lambda location: (39.17, -86.53, location))

    # target is exactly 4 hours after fixed_now (5:00 -> 9:00 UTC)
    weather.compute_weather("Indianapolis, IN", d0, hourly_at="09:00")
    assert captured["hours"] >= 5


def test_google_weather_request_maps_403_to_api_not_enabled(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    class FakeResponse:
        status_code = 403
        ok = False

    monkeypatch.setattr(weather.requests, "get", lambda url, params=None, timeout=None: FakeResponse())
    with pytest.raises(weather.WeatherError, match="weather_api_not_enabled"):
        weather._google_weather_request("https://example.invalid", {})
