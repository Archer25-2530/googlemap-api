"""Weather forecasts for get_weather.

Kept as its own module, separate from core.py, so a weather bug can't break
get_drive_time / get_route_eta / get_nearby_places. See
maps-mcp_get_weather_HANDOFF.md for the full spec this implements.

Call budget is the #1 requirement here (Drew has a hard Google Cloud limit):
this module makes at most 1 Google Weather "days" request, at most 1 Google
Weather "hours" request, and never follows nextPageToken. WEATHER_DAILY_CAP
is a hardcoded in-memory safety net on top of that, not a substitute for it.
"""
import datetime as dt
import math
import os
import sys
import zoneinfo

import requests

from . import google_client as gc

GOOGLE_WEATHER_DAYS_URL = "https://weather.googleapis.com/v1/forecast/days:lookup"
GOOGLE_WEATHER_HOURS_URL = "https://weather.googleapis.com/v1/forecast/hours:lookup"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

MAX_FORECAST_DAYS = 10  # Google Weather's forecast window
MAX_HOURLY_HOURS = 240  # 10 days, in hours
MAX_SPAN_DAYS = 21
TYPICAL_YEARS = 10
MIN_PRECIP_INCHES = 0.04

# Hardcoded, not an env var, per Drew's explicit ask: he never wants this
# tool anywhere near his Google Cloud request limit.
WEATHER_DAILY_CAP = 50
_google_call_budget = {"date": None, "count": 0}


class WeatherError(RuntimeError):
    """Raised for any failure computing a weather forecast."""


def _today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _zone(timezone_name: str | None) -> dt.tzinfo:
    if not timezone_name:
        return dt.timezone.utc
    try:
        return zoneinfo.ZoneInfo(timezone_name)
    except zoneinfo.ZoneInfoNotFoundError:
        return dt.timezone.utc


def _reserve_google_call_budget() -> None:
    """Raise before making a Google Weather request if today's cap is hit;
    otherwise count it and log so Portainer logs show the running total.
    """
    today = _today_utc()
    if _google_call_budget["date"] != today:
        _google_call_budget["date"] = today
        _google_call_budget["count"] = 0
    if _google_call_budget["count"] >= WEATHER_DAILY_CAP:
        raise WeatherError("daily_weather_cap_reached")
    _google_call_budget["count"] += 1
    print(
        f"weather call {_google_call_budget['count']}/{WEATHER_DAILY_CAP}",
        file=sys.stderr,
    )


def _google_weather_request(url: str, params: dict) -> dict:
    """GET a Google Weather endpoint. No retry on 4xx; at most 1 retry on
    5xx/timeout. Never follows nextPageToken.
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise WeatherError("weather_api_not_enabled")
    full_params = {**params, "key": api_key}

    attempt = 0
    while True:
        attempt += 1
        _reserve_google_call_budget()
        try:
            response = requests.get(url, params=full_params, timeout=10)
        except requests.RequestException as exc:
            if attempt > 1:
                raise WeatherError(f"weather_request_failed: {exc}") from exc
            continue

        if response.status_code == 403:
            raise WeatherError("weather_api_not_enabled")
        if 400 <= response.status_code < 500:
            raise WeatherError(f"weather_request_failed: {response.status_code}")
        if response.status_code >= 500:
            if attempt > 1:
                raise WeatherError(f"weather_request_failed: {response.status_code}")
            continue

        return response.json()


def _resolve_location(location: str) -> tuple[float, float, str]:
    """Return (lat, lng, label). Accepts "lat,lng" directly (0 requests) or
    geocodes free text via the Address Validation API (1 request).
    """
    parts = location.split(",")
    if len(parts) == 2:
        try:
            lat = float(parts[0].strip())
            lng = float(parts[1].strip())
            return lat, lng, location
        except ValueError:
            pass

    try:
        result = gc.validate_address(location)
    except gc.MapsClientError as exc:
        raise WeatherError("geocode_failed") from exc

    geocode_info = result.get("geocode", {})
    loc = geocode_info.get("location", {})
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if lat is None or lng is None:
        raise WeatherError("geocode_failed")
    label = result.get("address", {}).get("formattedAddress", location)
    return lat, lng, label


def _fetch_daily_forecast(lat: float, lng: float) -> dict:
    return _google_weather_request(
        GOOGLE_WEATHER_DAYS_URL,
        {
            "location.latitude": lat,
            "location.longitude": lng,
            "days": MAX_FORECAST_DAYS,
            "pageSize": MAX_FORECAST_DAYS,
            "unitsSystem": "IMPERIAL",
        },
    )


def _fetch_hourly_forecast(lat: float, lng: float, hours: int) -> dict:
    return _google_weather_request(
        GOOGLE_WEATHER_HOURS_URL,
        {
            "location.latitude": lat,
            "location.longitude": lng,
            "hours": hours,
            "pageSize": hours,
            "unitsSystem": "IMPERIAL",
        },
    )


def _extract_daily_entries(daily_response: dict) -> dict[str, dict]:
    entries = {}
    for day in daily_response.get("forecastDays", []):
        date_info = day.get("displayDate")
        if not date_info:
            continue
        date_str = "{:04d}-{:02d}-{:02d}".format(
            date_info["year"], date_info["month"], date_info["day"]
        )
        daytime = day.get("daytimeForecast", {})
        nighttime = day.get("nighttimeForecast", {})

        day_precip = daytime.get("precipitation", {}).get("probability", {}).get("percent") or 0
        night_precip = nighttime.get("precipitation", {}).get("probability", {}).get("percent") or 0

        snow_in = 0.0
        for forecast in (daytime, nighttime):
            precip = forecast.get("precipitation", {})
            if precip.get("precipitationType") == "SNOW":
                qpf = precip.get("qpf", {}).get("quantity")
                if qpf:
                    snow_in = max(snow_in, qpf)

        entries[date_str] = {
            "date": date_str,
            "high_f": round(day.get("maxTemperature", {}).get("degrees", 0)),
            "low_f": round(day.get("minTemperature", {}).get("degrees", 0)),
            "precip_chance_pct": round(max(day_precip, night_precip)),
            "snow_in": round(snow_in, 2),
            "summary": daytime.get("weatherCondition", {}).get("description", {}).get("text", ""),
            "source": "forecast",
        }
    return entries


def _extract_hour_temp(
    hourly_response: dict, target_date: str, hourly_at: str, timezone_name: str | None
) -> int | None:
    tz = _zone(timezone_name)
    hour, minute = (int(part) for part in hourly_at.split(":"))
    target_dt = dt.datetime.fromisoformat(target_date).replace(hour=hour, minute=minute, tzinfo=tz)

    best = None
    best_diff = None
    for entry in hourly_response.get("forecastHours", []):
        start = entry.get("interval", {}).get("startTime")
        if not start:
            continue
        start_dt = dt.datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz)
        diff = abs((start_dt - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = entry

    if best is None:
        return None
    degrees = best.get("temperature", {}).get("degrees")
    return round(degrees) if degrees is not None else None


def _fetch_typical_days(lat: float, lng: float, dates: list[str]) -> dict[str, dict]:
    """One Open-Meteo Archive request (not Google) covering the last
    TYPICAL_YEARS years; average same-calendar-date values across years for
    each requested date. Marked "typical" in the output per spec.
    """
    end = _today_utc() - dt.timedelta(days=2)  # archive data lags a couple of days
    try:
        start = end.replace(year=end.year - TYPICAL_YEARS)
    except ValueError:
        start = end.replace(year=end.year - TYPICAL_YEARS, day=end.day - 1)  # Feb 29 fallback

    try:
        response = requests.get(
            OPEN_METEO_ARCHIVE_URL,
            params={
                "latitude": lat,
                "longitude": lng,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum",
                "temperature_unit": "fahrenheit",
                "precipitation_unit": "inch",
                "timezone": "auto",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise WeatherError(f"typical_weather_request_failed: {exc}") from exc
    if not response.ok:
        raise WeatherError(f"typical_weather_request_failed: {response.status_code}")

    daily = response.json().get("daily", {})
    times = daily.get("time", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    precip = daily.get("precipitation_sum", [])
    snow = daily.get("snowfall_sum", [])

    by_month_day: dict[str, list[int]] = {}
    for i, date_str in enumerate(times):
        by_month_day.setdefault(date_str[5:], []).append(i)

    results = {}
    for date_str in dates:
        idxs = by_month_day.get(date_str[5:], [])
        year_highs = [highs[i] for i in idxs if i < len(highs) and highs[i] is not None]
        year_lows = [lows[i] for i in idxs if i < len(lows) and lows[i] is not None]
        year_precip = [precip[i] for i in idxs if i < len(precip) and precip[i] is not None]
        year_snow = [snow[i] for i in idxs if i < len(snow) and snow[i] is not None]
        if not year_highs or not year_lows:
            continue
        precip_days = sum(1 for p in year_precip if p >= MIN_PRECIP_INCHES)
        results[date_str] = {
            "date": date_str,
            "high_f": round(sum(year_highs) / len(year_highs)),
            "low_f": round(sum(year_lows) / len(year_lows)),
            "precip_chance_pct": round(100 * precip_days / len(year_precip)) if year_precip else 0,
            "snow_in": round(sum(year_snow) / len(year_snow), 2) if year_snow else 0.0,
            "summary": "typical",
            "source": "typical",
        }
    return results


def compute_weather(
    location: str,
    start_date: str,
    end_date: str | None = None,
    hourly_at: str | None = None,
) -> dict:
    end_date = end_date or start_date

    try:
        start = dt.date.fromisoformat(start_date)
        end = dt.date.fromisoformat(end_date)
    except ValueError as exc:
        raise WeatherError(f"invalid_date: {exc}") from exc
    if end < start:
        raise WeatherError("end_date before start_date")
    if (end - start).days > MAX_SPAN_DAYS:
        raise WeatherError(f"date span exceeds {MAX_SPAN_DAYS} days")

    lat, lng, label = _resolve_location(location)

    all_dates = [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]

    today = _today_utc()
    google_cutoff = today + dt.timedelta(days=MAX_FORECAST_DAYS - 1)
    google_dates = [d for d in all_dates if today <= dt.date.fromisoformat(d) <= google_cutoff]
    typical_dates = [d for d in all_dates if d not in google_dates]

    days_by_date: dict[str, dict] = {}
    timezone_name = None

    if google_dates:
        daily_response = _fetch_daily_forecast(lat, lng)
        timezone_name = daily_response.get("timeZone", {}).get("id")
        forecast_entries = _extract_daily_entries(daily_response)
        for d in google_dates:
            if d in forecast_entries:
                days_by_date[d] = forecast_entries[d]

    if typical_dates:
        days_by_date.update(_fetch_typical_days(lat, lng, typical_dates))

    if hourly_at:
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now + dt.timedelta(hours=MAX_HOURLY_HOURS)
        tz = _zone(timezone_name)
        hour, minute = (int(part) for part in hourly_at.split(":"))

        hourly_dates = []
        for d in google_dates:
            if d not in days_by_date:
                continue
            target_dt = dt.datetime.fromisoformat(d).replace(hour=hour, minute=minute, tzinfo=tz)
            if target_dt.astimezone(dt.timezone.utc) <= cutoff:
                hourly_dates.append(d)

        if hourly_dates:
            last_date = max(hourly_dates)
            target_dt = dt.datetime.fromisoformat(last_date).replace(hour=hour, minute=minute, tzinfo=tz)
            hours_needed = math.ceil((target_dt.astimezone(dt.timezone.utc) - now).total_seconds() / 3600)
            hours_needed = max(1, min(MAX_HOURLY_HOURS, hours_needed))

            hourly_response = _fetch_hourly_forecast(lat, lng, hours_needed)
            for d in hourly_dates:
                temp = _extract_hour_temp(hourly_response, d, hourly_at, timezone_name)
                if temp is not None:
                    days_by_date[d]["temp_at_hour_f"] = temp

    days = [days_by_date[d] for d in all_dates if d in days_by_date]

    return {
        "location": label,
        "lat": lat,
        "lng": lng,
        "timezone": timezone_name,
        "days": days,
    }
