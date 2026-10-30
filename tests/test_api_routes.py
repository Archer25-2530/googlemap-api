"""GET /api/* routes: per-consumer keys (header or query), /api/nearby-places
param parsing and error mapping. No Google calls."""
import pytest
from starlette.testclient import TestClient

from maps_mcp import server
from maps_mcp.google_client import MapsClientError

KEY = "test-key"
HA_KEY = "ha-key"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TINYNATURE_API_KEY", KEY)
    monkeypatch.setenv("HA_MAPS_KEY", HA_KEY)
    return TestClient(server.mcp.http_app())


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake(*args):
        calls.append(args)
        return {"summary": "ok", "places": []}

    monkeypatch.setattr(server, "compute_nearby_places", fake)
    return calls


def test_rejects_missing_or_bad_key(client, captured):
    assert client.get("/api/nearby-places?kind=food&origin=a").status_code == 401
    assert client.get("/api/nearby-places?kind=food&origin=a&key=nope").status_code == 401
    assert captured == []


def test_requires_kind_and_origin(client, captured):
    r = client.get(f"/api/nearby-places?key={KEY}&kind=food")
    assert r.status_code == 400


def test_passes_params_through(client, captured):
    r = client.get(
        "/api/nearby-places",
        params={
            "key": KEY,
            "kind": "food",
            "origin": "home",
            "destination": "site",
            "within_first_minutes": "60",
            "departure_time": "2026-10-29T08:00:00-04:00",
            "max_results": "8",
            "brands": "Chick-fil-A, Wawa",
        },
    )
    assert r.status_code == 200
    assert captured == [
        ("food", "home", "site", None, 8, 60, "2026-10-29T08:00:00-04:00", ["Chick-fil-A", "Wawa"])
    ]


def test_defaults_and_brands_none(client, captured):
    client.get(f"/api/nearby-places?key={KEY}&kind=gas&origin=home&brands=none")
    assert captured == [("gas", "home", None, None, 5, None, "now", [])]


def test_bad_int_is_400(client, captured):
    r = client.get(f"/api/nearby-places?key={KEY}&kind=food&origin=a&max_results=x")
    assert r.status_code == 400


def test_maps_error_is_502(client, monkeypatch):
    def boom(*args):
        raise MapsClientError("quota")

    monkeypatch.setattr(server, "compute_nearby_places", boom)
    r = client.get(f"/api/nearby-places?key={KEY}&kind=food&origin=a")
    assert r.status_code == 502


def test_header_key_and_ha_key(client, captured):
    url = "/api/nearby-places?kind=food&origin=a"
    assert client.get(url, headers={"X-API-Key": HA_KEY}).status_code == 200
    assert client.get(url, headers={"X-API-Key": KEY}).status_code == 200
    assert client.get(url + f"&key={HA_KEY}").status_code == 200
    assert client.get(url, headers={"X-API-Key": "nope"}).status_code == 401


def test_header_key_on_existing_route(client, monkeypatch):
    monkeypatch.setattr(server, "compute_drive_time", lambda *a: {"duration_seconds": 1})
    r = client.get(
        "/api/drive-time?origin=a&destination=b", headers={"X-API-Key": HA_KEY}
    )
    assert r.status_code == 200


def test_unset_ha_key_turns_ha_off(client, captured, monkeypatch):
    monkeypatch.delenv("HA_MAPS_KEY")
    url = "/api/nearby-places?kind=food&origin=a"
    assert client.get(url, headers={"X-API-Key": HA_KEY}).status_code == 401
    assert client.get(url, headers={"X-API-Key": ""}).status_code == 401


def test_caller_names_consumer(client, captured, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="maps_mcp.server"):
        client.get("/api/nearby-places?kind=food&origin=a", headers={"X-API-Key": HA_KEY})
    assert "by home-assistant" in caplog.text
    assert HA_KEY not in caplog.text
