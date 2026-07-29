# Geofence Departure Agent

Your phone quietly reports every geofence crossing. From that, this tells you
when to leave your dorm to make practice or lunch on time, asks whether you're
going, and gets better at the estimate every week — over Telegram.

```
🔔 Swim practice — leave in 15 min
Out the door by 5:45 AM to make denunzio by 6:00 AM.
Travel: ~9 min (from 23 trips).

        [ 🏃 On my way ]   [ 🙅 Skipping today ]
```

The whole premise is that you'd stop logging manually within three weeks, so
nothing asks you to record anything. You tap one button; everything else —
when you actually left, how long the walk took, whether you made it — is
measured from the geofence crossings that arrive on their own.

**Phase 1** is the ingestion + logging layer: capture, storage, segments.
**Phase 2** is the agent: departure alerts, the coffee planner, and the advice
that tells you to start leaving earlier or later.

---

## What it does

**Phase 1 — capture**

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

**Phase 2 — the agent**

- Telegram alerts before every commitment, with **On my way** / **Skipping
  today** buttons.
- Departure times computed from your **measured** travel times (p80 of recent
  trips), so they track your actual pace instead of a guess you typed once.
- One nudge if you said you were on your way and the geofence says you're
  still in your room.
- Silent grading of every occurrence — on time, late, no-show — which feeds
  `advice`: *leave earlier* or *leave later*, with the exact config change.
- `/coffee` on demand: the best window today that doesn't wreck your schedule.
- A **weekly digest** so the "leave earlier/later" verdict reaches you without
  being asked, with the numbers behind it.
- A **silent-data alarm**: if crossings stop arriving, you get told — over
  Telegram, once, with the fix — instead of finding a hole in the data weeks
  later.

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

## The agent: Telegram setup

### 1. Make the bot

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot`.
2. Give it a name and a username. BotFather replies with a **token** —
   `123456789:AAF...`. Anyone holding that token *is* your bot, so treat it
   like a password.
3. **Message your new bot** (send it anything). A bot can't start a
   conversation with you, so until you do, it has nowhere to send alerts.
4. Get your chat id:

   ```bash
   curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
   ```

   Read `result[0].message.chat.id` out of the JSON.

5. Put both in `geofence.env` (the same file as `GEOFENCE_SECRET`):

   ```
   TELEGRAM_BOT_TOKEN=123456789:AAF...
   TELEGRAM_CHAT_ID=987654321
   ```

6. Confirm the round trip:

   ```bash
   export $(cat geofence.env) && python -m geofence telegram test
   ```

Only that chat id can drive the bot. Anyone else who finds your bot's username
gets ignored (and logged), because a stranger tapping "skipping today" for you
would be a genuinely bad day.

No inbound port is needed: the agent long-polls Telegram over an outbound
connection, so it runs behind any dorm NAT.

### 2. Describe your commitments

In `geofence.toml`. Two are pre-filled — practice and lunch:

```toml
[[commitments]]
id                  = "swim_practice"
label               = "Swim practice"
origin              = "henry_hall"      # you leave FROM here
destination         = "denunzio"        # you must arrive HERE
arrive_by           = "06:00"           # LOCAL wall clock
days                = ["mon", "tue", "wed", "thu", "fri"]
prep_sec            = 900               # how much warning you want
safety_margin_sec   = 300               # cushion beyond the measured walk
fallback_travel_sec = 600               # used only until data exists
```

`arrive_by` is local wall-clock time, resolved per-date, so the morning the
clocks change, 6:00am practice is still at 6:00am.

Bad region ids and bad day names are rejected **when the config loads**, not at
5:30am — a typo that silently cancels your alarm is exactly the failure this
project exists to prevent. Check yours with `python -m geofence plan`.

### 3. Run it

```bash
export $(cat geofence.env)
python -m geofence agent          # foreground, for testing
```

Then install the unit so it survives reboots — same pattern as the ingestion
server, and both can run side by side:

```bash
sudo cp systemd/geofence-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now geofence-agent
journalctl -u geofence-agent -f
```

---

## How the departure time is worked out

```
must_leave = arrive_by − travel_estimate − safety_margin_sec
ping_at    = must_leave − prep_sec
```

**`travel_estimate` is measured and adapts on its own.** It's the 80th
percentile of your recent trips on that exact leg (`henry_hall → denunzio`),
over a rolling 60-day window. You never edit it. As your pace changes — a
faster route, a snowy February, a knee — every future departure time moves with
it within days.

Why p80 and not the average: you don't care about your typical walk to the
pool, you care about not being late. At p80, roughly one trip in five runs
longer than the estimate, and `safety_margin_sec` is what absorbs those.

**Before there's data**, the estimate falls back to `fallback_travel_sec` and
the message says so — "*~10 min (estimate — not enough data yet)*". A
confident-looking number derived from two walks would be a lie, and you'd learn
to distrust the whole thing.

**Incomplete and suspect segments never count.** The day your phone died, or
the day you got a ride, is an `incomplete` segment with no duration — so it can
never be averaged in as a four-hour walk and push your alarm 20 minutes earlier.

## Your answer, and what happens after it

| You tap | Effect |
|---|---|
| **🏃 On my way** | Logged. If the geofence still has you in the dorm 5 min past your departure time, you get **one** nudge — never a stream of them. |
| **🙅 Skipping today** | Logged and closed. No nudge, and — importantly — **it never counts as a late arrival**, so choosing to skip can't slowly push your departure times earlier for no reason. |
| *nothing* | Graded from the geofence data anyway: you either arrived (on time / late) or you didn't (`no_show`, kept separate from `late` so a forgotten tap isn't read as chronic lateness). |

The buttons can be awkward on a locked screen, so typing `omw` or `skip` logs
the same thing against the open alert.

**If the agent was down** over a departure time, it does *not* ping you late —
a "leave now" that lands after you should already have gone is worse than
silence. The occurrence is recorded as `missed_window` so the gap stays visible
in your data, and it's excluded from advice (it measures the server, not you).

## Leave earlier or later?

```bash
python -m geofence advice        # or /advice in Telegram
```

```
Swim practice:
  Leave earlier: late 40% of the last 10 trips (expected ~20% at p80).
  → geofence.toml: safety_margin_sec 300 → 540
```

The travel estimate already adapts by itself. What `advice` tunes is the two
knobs that encode *preference* rather than fact:

- **`safety_margin_sec`** — how much cushion you want. Late more often than the
  percentile predicts → too thin. Never once used it, and habitually 20 minutes
  early → too fat, and you're standing around at the pool.
- **`prep_sec`** — if you consistently walk out five minutes after the ping,
  the ping should come five minutes sooner.

It stays quiet until there are `advice_min_occurrences` (default 8) real
occurrences, and it *suggests* rather than rewrites — these are your
preferences, so the edit is yours to make.

## Coffee

```bash
python -m geofence coffee        # or /coffee in Telegram
```

```
☕ Best time: 7:35 AM (from denunzio)
   Round trip 14 min + 10 min in the shop.
   Set off by 11:48 AM to still make Lunch at the club.
```

It plans the whole round trip — out, queue, back — against the same measured
travel times, and checks it against every gap left in today's schedule: right
now from wherever you currently are, and after each commitment. "After
practice" starts from *your* median stay at DeNunzio, measured, not from an
assumption about how long practice runs. The earliest window that fits wins,
and the answer always names the commitment that bounds it, so the constraint is
visible rather than mysterious. If nothing fits, it says so and why.

## The weekly digest

Sunday at 19:00 by default (`[digest]` in `geofence.toml`; `/digest` any time):

```
📊 Your week

Swim practice
  On time 4/5, skipped 1
  Walk: typically 7 min, planned on 9 min (p80 of 23 trips)
  Timing looks right — late 20% of 10 trips, typically 3 min to spare.

Lunch at the club
  On time 5/5
  Walk: typically 6 min, planned on 7 min (p80 of 19 trips)
  You could leave later: typically 12 min early, and never late in 11 trips.
  → geofence.toml: safety_margin_sec 300 → 180
```

It reports the median *and* the p80 the alerts actually plan on, so you can see
the gap between your typical walk and the one being planned for. If more than a
third of the week's segments were incomplete or suspect, it says so — a week
where the data didn't record is a week whose numbers you shouldn't act on.

Sent at most once per week, keyed on the most recent send moment that's passed.
If the box was off all Sunday evening and boots Monday, you get that week's
digest late rather than never — and a machine off for three weeks sends one
current digest, not a backlog. Installing the agent doesn't fire one
immediately; the first real send is the first Sunday after setup.

## If the data stops

This is the failure that would actually kill the project. Nothing visibly
breaks: alerts keep firing on stale estimates and every occurrence quietly
grades as `no_show`, so you'd find out weeks later, from a hole in the data.

The agent watches for it and tells you:

```
⚠️ No geofence data for 9h.
Departure times are running on stale estimates, and today's commitments
will grade as no-shows.
Check the Shortcut automations are still set to Run Immediately (iOS turns
them off after some updates), and that Location access is still Always.
Last seen at: Henry Hall.
```

Two rules keep it from becoming noise. You're told **once**, then not again for
`silence_repeat_hours` (default 12) — a warning that repeats every 20 seconds
is one you learn to swipe away. And when data resumes you get a **recovery**
message, which is what makes the warning trustworthy: silence from the bot then
genuinely means the data is flowing, not that it gave up.

A brand-new install with no events ever recorded is *not* treated as an
incident — that's "not set up yet", not "something broke".

`python -m geofence doctor` still exits non-zero for cron, and remains the
right tool for machine monitoring.

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

python -m geofence agent                        # run the Telegram alerting loop
python -m geofence agent once [--no-poll]       # a single tick (cron / testing)
python -m geofence plan [--days N]              # computed departure times
python -m geofence coffee                       # best coffee window today
python -m geofence advice                       # leave earlier or later?
python -m geofence digest [--send]              # this week's summary
python -m geofence telegram test                # confirm the bot works
```

### Telegram commands

`/today` · `/next` · `/coffee` · `/where` · `/advice` · `/digest` · `/stats` ·
`/help`, plus `omw` and `skip` as typed equivalents of the buttons.

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
- `alerts` — one row per commitment-occurrence: what was planned, what was
  sent, what you answered, and how it graded.
- `bot_state` — the Telegram update cursor, so a restart neither loses your
  button tap nor replays it.

Because `events` and `segments` are fully derived, `reprocess` wipes and rebuilds
them from `raw_events` — that's how you re-derive everything when your logic
changes. `alerts` is *not* derived: it's the record of what you were actually
told and what you actually answered, so it survives reprocessing untouched.

### Can it ping me twice?

No, and the mechanism is worth knowing. `alerts` has a
`UNIQUE(commitment_id, occurrence_date, kind)` constraint, and the row is
claimed with `INSERT OR IGNORE` **before** the Telegram call is made. A restart
loop, two agents started by mistake, or a tick that runs twice in the same
second all lose the race and send nothing. If the send itself fails, the claim
is released so the next tick retries — within the send window only.

---

## Tests

```bash
pip install pytest
python -m pytest
```

101 tests, no network and no waiting for 5:37am — the whole tick takes an
injected `now`, and Telegram is a fake transport.

| File | Covers |
|---|---|
| `test_debounce.py` | boundary-flap suppression |
| `test_segments.py` | incomplete-pair exclusion, overlapping-geofence guard |
| `test_normalize.py` | both client payload formats |
| `test_schedule.py` | departure maths, DST, p80 estimation, cold start |
| `test_agent.py` | send-once, retry, missed windows, responses, nudges, auth |
| `test_grading_advice.py` | outcome grading, earlier/later advice |
| `test_coffee.py` | window feasibility against the day's schedule |
| `test_telegram.py` | payload shaping, update parsing, failure handling |
| `test_health_digest.py` | silence alarm + recovery, weekly digest scheduling |
