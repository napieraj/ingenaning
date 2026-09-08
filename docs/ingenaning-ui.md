# ingenaning — UI (minimum viable)

One page, served by the daemon itself on `:8080` inside the container. No reverse proxy, no auth, no build step — reachable only on the storage VLAN, which is the security model for now. Its job is to show what moved and let you say whether that was right.

## 1. Stack

Plain HTML plus a single `app.js` using `htmx` for partials and the daemon's WebSocket for the live feed. No framework, no bundler. FastAPI serves the static files and Jinja partials from `ingenaning/api/templates/`.

## 2. The page

Top to bottom, single column, works on a phone.

**Status line**
`hot 2.9 / 4.0 TB · cold ok · presence: approaching (12s) · last run 03:31 · degraded: no`

**Feed** — the last 100 events, newest first, streamed over the WebSocket:

```
20:14  ▲ /media/tv/severance/S02E03.mkv        next-episode   ✓ ✕
20:14  ▲ /media/tv/severance/S02E04.mkv        p-media        ✓ ✕
19:50  ▼ /rips/2026-09-05-disc-14/             age            ✓ ✕
19:12  ✗ /projects/lisbon-2026/raw/            p-project      rejected: budget
18:40  ○ /media/film/heat.mkv                  miss           pin
```

`▲` promoted, `▼` demoted, `✗` rejected, `○` opened cold. Path truncated from the left. Tapping a row expands one line: full reason, arm, bucket, size, and a `why` link that lists every arm that scored the file and what it gave it.

`✓` and `✕` post feedback. `pin` on a miss creates a hot pin with no expiry. All three are single POSTs; the row updates in place.

**Run** — one form: arms (checkboxes, default all), window hours, filter glob, dry-run toggle, `run`. A dry run appends its plan to the feed as `?` rows with the same `✓ ✕` controls; `✓` on a `?` row moves that file now.

**Pins** — the current list with `until` and a `remove` link; an add form with path, tier, optional until.

**Intent** — a textarea over `intent.md` with `save` and `compile now`; the compiled rules print beneath it as plain text.

**Arms** — a table: name, enabled checkbox, 7-day hit rate, proposals, bytes moved. Nothing else.

**Metric** — one number, large: first-open-on-hot rate, 7-day mean, with the daily values for the last 14 days as a row of small numbers under it.

## 3. What is deliberately absent

Schedule grid, file browser, signal management, sparklines, notifications, auth, dark mode. Schedules and signal definitions are edited in `policy.yaml` and via the API with `curl` until the page above has earned an extension. Nothing on the page holds state the daemon needs.

## 4. Endpoints used

`GET /` · `WS /ws/events` · `POST /api/proposals/{id}/feedback` · `POST /api/pins` · `DELETE /api/pins/{id}` · `GET/PUT /api/intent` · `POST /api/intent/compile` · `POST /api/run` · `POST /api/arms/{name}/enabled` · `GET /api/metrics?days=14`

All already defined in `ingenaning-software-build.md`.

## 5. Build

An afternoon. Feed and feedback first; if that's all that ever ships, the loop still closes.
