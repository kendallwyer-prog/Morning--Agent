# Geofence Ingestion (Phase 1)

Automatic capture of geofence crossings from your phone, stored immutably and
turned into travel/dwell **segments**. This is the ingestion + logging layer
**only** — no alerting, no advice, no scheduling math. That's later phases.

The whole premise is that you'll stop logging manually within three weeks, so
capture is 100% automatic: your phone POSTs a tiny JSON blob every time you
cross a geofence, and this service records it.

---

## What it does

- One authenticated HTTP endpoint accepts **both** iPhone Shortcuts JSON **and**
  OwnTracks `transition` payloads, normalizing them into one internal shape.
- Stores **every raw event immutably**, exactly as received, before any
  processing — so you can re-derive everything later when your logic changes.
- Derives two kinds of **segments**:
  - `transit` — exit-from-A / enter-at-B, duration = travel time between places.
  - `dwell` — enter-at-A / exit-at-A, duration = time spent somewhere.
- Handles the three known failure modes explicitly (debounce, broken pairs,
  systematic iOS bias — see [Design notes](#design-notes)).
- Stores UTC **and** local time, DST-correct.
- Dedupes double-fires on a client-supplied (or synthesized) UUID.
- CLI: `stats`, `doctor`, `calibrate`, `reprocess`.

---

## Regions

`henry_hall` (dorm), `denunzio` (pool), `prospect_club` (dining),
`firestone` (library), `nassau_starbucks`. Defined in `geofence.toml` with ~100m
radius each. `henry_hall` and `firestone` overlap, so fast transitions between
them are flagged `suspect` and excluded from stats.

---

## Install & run on a Pi / $5 VPS

Requires Python 3.11+ (for `zoneinfo` + `tomllib`). Data stays on your box; no
cloud, no analytics.

```bash
git clone <your repo> && cd Morning--Agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-geofence.txt

# 1. Create the shared secret (every client must send this).
python3 -c "import secrets; print('GEOFENCE_SECRET=' + secrets.token_urlsafe(32))" > geofence.env
chmod 600 geofence.env

# 2. Create the database schema.
export $(cat geofence.env) && python -m geofence initdb

# 3. Run the server (foreground, for testing).
python -m geofence serve --host 0.0.0.0 --port 8000
```

Health check: `curl http://localhost:8000/health` → `{"status":"ok"}`.

### Survive reboots (systemd)

```bash
# Edit systemd/geofence.service: set User=, WorkingDirectory=, ExecStart python path.
sudo cp systemd/geofence.service /etc/systemd/system/geofence.service
sudo systemctl daemon-reload
sudo systemctl enable --now geofence
systemctl status geofence
journalctl -u geofence -f     # live logs
```

`Restart=always` + `WantedBy=multi-user.target` means it comes back after a
crash or reboot.

### Exposing it to the internet

This is a **public endpoint**, protected only by the `X-Auth-Token` shared
secret. Put it behind HTTPS — a reverse proxy (Caddy/nginx) with a real
certificate, or a tunnel (Tailscale Funnel / Cloudflare Tunnel). Never send the
secret over plain HTTP. Requests with a missing/wrong token get `401` and are
**not stored**, so random internet traffic can't fill your disk.

---

## iPhone Shortcuts setup (exact steps)

You'll create **two** Personal Automations per region — one for **Arrive**, one
for **Leave**. It's repetitive; do `henry_hall` first, confirm data lands, then
copy the pattern.

### The critical toggle

> **⚠️ "Run Immediately" (formerly "Ask Before Running" = OFF).**
> A location automation that asks before running will sit silently waiting for a
> tap you'll never give — and the whole automatic-capture premise breaks. Every
> automation below **must** be set to run immediately with notifications off.

### Steps (for ONE region + ONE direction, e.g. henry_hall / Arrive)

1. Open **Shortcuts** → **Automation** tab → **+** (top right) → **Create
   Personal Automation**.
2. Choose **Arrive** (or **Leave** for the exit automation).
3. **Location** → search and drop the pin on the region (e.g. Henry Hall) →
   set the radius. Tap **Done**.
4. Tap **Next**.
5. Add action **Get Contents of URL**. Expand **Show More** and set:
   - **URL**: `https://your-server.example/event`
   - **Method**: `POST`
   - **Headers**: add one → key `X-Auth-Token`, value = your `GEOFENCE_SECRET`.
   - **Request Body**: `JSON`. Add these fields:

     | Key | Type | Value |
     |-----|------|-------|
     | `region` | Text | `henry_hall` |
     | `event` | Text | `arrive` (or `leave`) |
     | `timestamp` | Text | tap the field → magic-variable → **Current Date** |
     | `uuid` | Text | tap → magic-variable → search **UUID** → insert |

   > `region` must match a region id (or alias) in `geofence.toml`. The `uuid`
   > gives idempotency — Shortcuts can double-fire, and a per-run UUID makes the
   > duplicate a harmless no-op. `timestamp` as **Current Date** is sent as
   > ISO-8601 and parsed server-side.

6. Tap **Next**.
7. **Turn OFF "Ask Before Running"** (newer iOS: toggle **Run Immediately** ON).
   Confirm the "Don't Ask" prompt. Optionally turn **Notify When Run** off too.
8. Tap **Done**.
9. Repeat for **Leave** at the same region, and then for all five regions
   (10 automations total).

### Confirm it works

After setting up one region, physically cross it (or use Shortcuts' "run" on the
automation for a smoke test), then:

```bash
python -m geofence doctor    # should show a recent "last event"
python -m geofence stats     # buckets fill in as you accumulate crossings
```

### OwnTracks alternative

If you'd rather use OwnTracks: set it to **HTTP** mode, point the URL at
`https://your-server.example/event`, add an HTTP header `X-Auth-Token: <secret>`,
and define Regions with names/descriptions matching the region ids or aliases
(e.g. "Henry Hall"). OwnTracks posts `_type: transition` on enter/leave; the
same endpoint accepts it. OwnTracks carries no client UUID, so the server
synthesizes a stable dedupe key from `tid + tst + event + region`.

---

## CLI reference

All commands read `geofence.toml` (override with `--config`).

```bash
python -m geofence serve [--host H --port P]   # run the ingestion server
python -m geofence initdb                       # create schema
python -m geofence reprocess                    # re-derive events+segments from raw
python -m geofence stats [--min-n N]            # median / p80 / n per segment type
python -m geofence doctor                       # "have events stopped arriving?" (exit 1 if silent)
python -m geofence calibrate add <region> <time> [--note ...]
python -m geofence calibrate estimate           # suggest per-region offsets
```

### `stats`

Median, 80th percentile, and n for each segment type. Only `complete` segments
count — `incomplete` and `suspect` are excluded. Any bucket with **n < 10** is
suppressed rather than shown as a noisy number (tune with `--min-n`).

### `doctor`

Reports the age of the most recent event, per-region last-seen, and counts of
incomplete/suspect segments. **Exits non-zero** if nothing has arrived within
`silence_threshold_hours` — wire it into cron/monitoring so a silently-stopped
Shortcut becomes a loud alert instead of a hole in your data:

```
0 * * * * cd /home/pi/Morning--Agent && python -m geofence doctor || notify-send "geofence silent"
```

### `calibrate` (week one)

iOS fires "leave" late (by 20–60s and 100+ metres). During week one, whenever
you actually leave somewhere, note the real time and record it:

```bash
python -m geofence calibrate add henry_hall "2026-03-01T08:31:00-05:00" --note "left for class"
```

After a week, get suggested offsets and paste them into
`geofence.toml` under each region's `calibration_offset_sec`, then run
`python -m geofence reprocess`:

```bash
python -m geofence calibrate estimate
```

---

## Design notes

The three problems you flagged, and how each is handled:

1. **Boundary flapping** — standing near an edge yields enter/exit/enter/exit.
   An exit within `flap_window_sec` (default **90s**, config) of an enter for the
   **same** region is discarded as a flap. (`geofence/processing/debounce.py`,
   tested in `tests/test_debounce.py`.)

2. **Broken pairs** — a dead phone / missed geofence / a day you got a ride
   leaves an exit with no plausible arrival. Any exit whose next arrival is
   further than `max_transit_sec` (default **30 min**) away — or absent entirely
   — becomes an `incomplete` segment with **no duration**, **excluded from
   stats**. One bad day can never be counted as a four-hour walk or poison a
   median. (`geofence/processing/segments.py`, tested in
   `tests/test_segments.py`.)

3. **Systematic bias** — per-region `calibration_offset_sec` corrects the late
   iOS "leave", applied to exit times when deriving segments. The `calibrate`
   CLI lets you enter ground truth in week one to estimate it.

**Overlapping geofences** — `henry_hall` and `firestone` are close, so their
100m circles overlap and produce near-instant "transitions". A crossing faster
than the per-pair `min_transit` (default **120s**) is flagged `suspect` and
excluded from stats.

### Data model (3 tables + ground truth)

- `raw_events` — **immutable**, exact payload, written before any processing.
- `events` — normalized, **derived** from `raw_events` (region, transition, UTC
  + local time, DST offset, calibrated time, suppressed flag).
- `segments` — **derived** matched pairs (`transit` / `dwell`) with status
  `complete` / `incomplete` / `suspect`.
- `ground_truth` — your manually-entered departure times for calibration.

Because `events` and `segments` are fully derived, `reprocess` wipes and rebuilds
them from `raw_events` — that's how you re-derive everything when your logic
changes.

---

## Tests

```bash
pip install pytest
python -m pytest tests/test_debounce.py tests/test_segments.py tests/test_normalize.py
```

`test_debounce.py` covers flap suppression; `test_segments.py` covers the
incomplete-pair exclusion and the overlapping-geofence guard.
