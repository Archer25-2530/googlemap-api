# Session: maps-mcp Cloudflare wiring + tinyNature GET endpoints

## What this session accomplished

### 1. Wired maps-mcp into the Cloudflare/Zero Trust stack
Replicated the same pattern used by ha-mcp, fs-mcp, pihole-mcp, portainer-mcp, proxmox-mcp:

- **Tunnel ingress**: `maps-mcp-internal.drewhobick.com` → `http://192.168.0.201:3000` added to the `homelab` cfd_tunnel, with a matching CNAME DNS record.
- **KV namespace**: `MAPS_MCP_OAUTH_KV`.
- **OAuth Worker**: `maps-mcp-oauth-worker`, source at `C:\Users\hobic\maps-mcp-oauth-worker` (pushed to `Archer25-2530/maps-mcp-oauth-worker`, private, `main`). Cloned from `ha-mcp-oauth-worker`'s pattern — Cloudflare Access gates `/login`, `@cloudflare/workers-oauth-provider` handles OAuth 2.1 for Claude, authenticated `/mcp` requests proxy to the internal tunnel hostname.
- **Custom domain**: `maps-mcp.drewhobick.com` → the Worker.
- **Access app**: self-hosted app gating only `maps-mcp.drewhobick.com/login`, policy allows `hobick54@gmail.com`.
- **Worker secrets**: `ALLOWED_EMAIL`, `UPSTREAM_URL` (`https://maps-mcp-internal.drewhobick.com/mcp`), `COOKIE_SECRET`.

Result: `https://maps-mcp.drewhobick.com/mcp` is live, OAuth-gated, verified against the real Google Maps API (Bloomington→Fort Wayne, Bloomington→Elkhart both matched build-spec baselines).

### 2. Added GET-only HTTP endpoints for tinyNature
tinyNature's executors can only issue GET requests, so they can't use the OAuth-gated `/mcp` route. Added a second, unauthenticated-at-the-Worker-layer path:

- **Worker** (`src/auth-handler.ts`): any `/api/*` path is proxied straight through to `https://maps-mcp-internal.drewhobick.com` with no OAuth check — the origin server enforces its own auth.
- **Origin** (`maps_mcp/server.py`, via FastMCP's `@mcp.custom_route`): four endpoints, each gated by a dedicated `TINYNATURE_API_KEY` query param (constant-time compare, `hmac.compare_digest`) — deliberately not the real `GOOGLE_MAPS_API_KEY`, so a leaked query-string key never touches the billable Google key.
  - `GET /api/drive-time?origin=&destination=&key=`
  - `GET /api/places?origin=&destination=&keyword=&type=&key=`
  - `GET /api/distance-matrix?origins=a|b&destinations=a|b&key=`
  - `GET /api/geocode?address=&key=` — backed by the **Address Validation API** (swapped from Geocoding API per Drew's preference), returns `formatted`, `lat`, `lng`, `place_id`, `complete`, `unconfirmed_components`.

Instructions + the live `TINYNATURE_API_KEY` value are in `C:\Users\hobic\tiny-maps-mcp-instructions.md` (outside this repo, not committed anywhere — keep it that way).

### 3. Portainer gotcha (important for next time)
The Portainer stack (id 33 as of this session — it was recreated once, so the id may drift again) is git-linked to `Archer25-2530/googlemap-api` branch `claude/google-maps-mcp-container-d2tgwy`, compose path `docker-compose.yml`.

Two real bugs hit during this session, now worked around but not root-caused:

1. **`StackGitRedeploy` always re-clones `main`** (the stub branch) instead of the stack's configured `ReferenceName`, wiping `/data/compose/<id>/` and leaving only `main`'s 15-byte README — breaks the compose build every time. Root cause unconfirmed (looks like a Portainer CE bug in git-ref resolution during pull).
2. **`StackUpdate` without an explicit `Env` array wipes all env vars** (`GOOGLE_MAPS_API_KEY`, `TINYNATURE_API_KEY`, `PORT`, `TZ`) rather than leaving them untouched. This also had the side effect of **converting the stack from git-tracked to file-based** (`GitConfig` became `null`), so redeploying via the Portainer UI no longer pulls from GitHub — it just rebuilds whatever's frozen on disk at `/data/compose/33/`.

**Current working pattern** for shipping code changes to this stack:
1. Push the change to the `claude/google-maps-mcp-container-d2tgwy` branch on GitHub as normal.
2. Manually sync `/data/compose/33/` with the latest commit: spin up a short-lived `alpine/git` container with `HostConfig.Mounts[].VolumeOptions.Subpath = "compose/33"` on the `portainer_data` volume (scopes access to only that stack's folder, nothing else in Portainer's data), `git clone` the branch into a temp path, `cp -a` it over `/fix` — explicitly preserving `stack.env` (Portainer's own env file for this now-file-based stack; back it up before the copy, restore after). Never `cat`/print `stack.env`'s contents.
3. Ask Drew to click **Update the stack** in the Portainer UI (Stacks → maps-mcp → Editor). This rebuilds from the now-updated on-disk source and correctly preserves env vars (the UI pre-fills them; only the raw API calls above have the wiping bug).
4. Verify via `curl` against the public endpoints.

Don't use `StackGitRedeploy` or `StackUpdate` via the API on this stack until the underlying Portainer bug is understood — both have bitten us.

### 4. Billing safety (not yet actioned)
Google Cloud has no hard "never charge me" switch — budgets only alert. Follow-up task created in Todoist ("Homelab" project): "Set up Google Maps API billing quotas (maps-mcp)" — per-API daily request quotas, a budget alert, and API-key restriction to the 4 APIs in use. Task ID `6h4hvPC4qJ2M6q4m`.

## Reference: what's live right now

| Endpoint | Auth | Status |
|---|---|---|
| `POST https://maps-mcp.drewhobick.com/mcp` | OAuth 2.1 (Claude connector) | ✅ |
| `GET https://maps-mcp.drewhobick.com/api/drive-time` | `key=` query param | ✅ |
| `GET https://maps-mcp.drewhobick.com/api/places` | `key=` query param | ✅ |
| `GET https://maps-mcp.drewhobick.com/api/distance-matrix` | `key=` query param | ✅ |
| `GET https://maps-mcp.drewhobick.com/api/geocode` | `key=` query param | ✅ |

Repos:
- `Archer25-2530/googlemap-api` (server, branch `claude/google-maps-mcp-container-d2tgwy` is what's deployed — `main` is just a stub, don't deploy from it)
- `Archer25-2530/maps-mcp-oauth-worker` (Cloudflare Worker, `main`)
