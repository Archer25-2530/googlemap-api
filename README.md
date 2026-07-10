# maps-mcp

MCP server wrapping the Google Maps Directions API. Gives tinyNature executor
agents real drive times instead of hardcoded guesses (e.g. Bloomington ->
Fort Wayne was guessed at 2h, actually ~3h).

Same deployment architecture as the existing `ha-mcp`, `fs-mcp`, and
`pihole-mcp` servers:

```
[MCP Server on Proxmox VM/LXC]
    -> [Cloudflare Tunnel (cloudflared)]
    -> [Public subdomain: maps-mcp.drewhobick.com]
    -> [Cloudflare Zero Trust Access — same policy as ha-mcp/fs-mcp/pihole-mcp]
    -> [OAuth Worker — same pattern as existing MCPs]
    -> [tinyNature executor agents hit the public endpoint]
```

## Tools

### `get_drive_time(origin, destination, departure_time="now")`

Single A -> B drive time, traffic-adjusted. Used for single-leg travel blocks
(home -> site visit).

```json
{
  "duration": "2h 47m",
  "duration_seconds": 10020,
  "distance": "187 mi",
  "origin": "4054 W Boheny Dr, Bloomington, IN",
  "destination": "815 Wernsig Rd, Jasper, IN",
  "traffic_adjusted": true
}
```

### `get_route_eta(waypoints, departure_time="now")`

Multi-stop routing. Used for home -> daycare -> site visit, or site visit ->
lunch -> second site.

```json
{
  "total_duration": "3h 22m",
  "total_distance": "215 mi",
  "arrival_time": "2026-07-28T10:15:00-04:00",
  "legs": [
    {"from": "...", "to": "...", "duration": "12m", "distance": "5.2 mi"},
    {"from": "...", "to": "...", "duration": "3h 10m", "distance": "210 mi"}
  ],
  "traffic_adjusted": true
}
```

`departure_time` accepts `"now"` or an ISO 8601 timestamp. Results are cached
for 5 minutes (per address/waypoint combination) to stay well under Directions
API rate limits.

## Environment variables

| Variable              | Required | Default | Notes                                      |
|-----------------------|----------|---------|---------------------------------------------|
| `GOOGLE_MAPS_API_KEY` | yes      | —       | Scope this key to the Directions API only.   |
| `PORT`                | no       | `3000`  | Port the MCP HTTP server listens on.         |
| `TZ`                  | no       | UTC     | Set so `"now"`/`arrival_time` use local time.|

Copy `.env.example` to `.env` and fill in your key.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
GOOGLE_MAPS_API_KEY=... python -m maps_mcp
```

Run tests:

```bash
pytest
```

Validate against the known baseline routes (requires a real API key and network access):

```bash
GOOGLE_MAPS_API_KEY=... python scripts/validate_routes.py
```

## Known drive times (validation baselines)

If the API returns something wildly different from these, something is wrong
(wrong address, wrong API enabled, bad key scope, etc.):

| Route                              | Expected duration |
|-------------------------------------|--------------------|
| Bloomington -> Elkhart, IN           | ~3.5h              |
| Bloomington -> Fort Wayne, IN        | ~3h                 |
| Bloomington -> Jasper, IN            | ~1h 15m             |
| Bloomington -> Erlanger, KY          | ~2h 15m             |
| Bloomington -> Indianapolis          | ~1h                 |
| Bloomington -> Kid Angles Daycare    | ~12m                |

## Container

```bash
docker compose up -d --build
```

Builds from `Dockerfile` and runs `python -m maps_mcp`, serving MCP over
streamable HTTP on `PORT` (default 3000).

## Deployment checklist

- [ ] Create Google Cloud project, enable the Directions API, generate an API key scoped to it
- [ ] Clone this repo to the Proxmox host (same host as the other MCPs)
- [ ] `docker compose up -d --build` (or run bare with systemd — match your existing setup)
- [ ] Add an ingress rule to your existing cloudflared tunnel config for `maps-mcp.drewhobick.com` -> `http://localhost:3000` (see `cloudflared/config.example.yml` — copy the real tunnel ID/credentials from ha-mcp/fs-mcp/pihole-mcp)
- [ ] Apply the existing Zero Trust Access policy to the new hostname
- [ ] Deploy the OAuth Worker in front of it (copy the pattern from ha-mcp/fs-mcp/pihole-mcp)
- [ ] `python scripts/validate_routes.py` against known routes
- [ ] Test `get_route_eta` with a multi-stop route
- [ ] Update tinyNature agent instructions to call maps-mcp
- [ ] Verify the morning briefing pulls real drive times

## Notes

- The Google Maps API key should be scoped to only the Directions API.
- Results are cached for 5 minutes to avoid hitting rate limits.
- `traffic_adjusted` tells callers whether Google returned a live-traffic
  duration (it may fall back to static duration if traffic data isn't
  available for a route).
- Departure time matters a lot for site visits — a 6am vs 8am departure on
  I-69 can differ significantly. Always pass a real `departure_time` when
  the agent knows the planned departure.
