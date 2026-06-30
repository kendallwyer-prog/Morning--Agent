# ☀️ Morning Agent

A daily "morning briefing" agent that sends **one push notification** to your
iPhone/iPad every morning with:

1. **Quote** — inspiring/interesting, no repeats within 30 days
2. **Song** — artist + track with a one-line reason
3. **News** — top 3–4 headlines across world / business / technology
4. **Portfolio** — Robinhood total value, day's gain/loss ($ and %), per-position
5. **Ideas** — 1–3 stock suggestions from a transparent momentum screen
   *(not financial advice — for informational purposes only)*

It runs as a free **scheduled GitHub Action**, delivers via **ntfy.sh**, and is
built so that **if any one section fails, the notification still sends** with the
rest, plus a note about what failed.

---

## How it works

```
GitHub Action (daily ~7am ET)
        │
        ▼
python -m morning_agent.main
        │  builds each section in its own try/except
        ├── quote      (Quotable API → curated fallback, 30-day no-repeat)
        ├── song       (curated rotating list, 30-day no-repeat)
        ├── news       (GNews top-headlines per category)
        ├── portfolio  (robin_stocks + TOTP 2FA)   ← fragile, degrades gracefully
        └── ideas      (yfinance momentum screen within your sectors)
        │
        ▼
POST https://ntfy.sh/<your-topic>
        │
        ▼
ntfy iOS app on iPhone + iPad  →  push notification
```

State for the 30-day no-repeat logic lives in `data/history.json`, which the
Action commits back to the repo after each run (the runner is ephemeral).

---

## Project structure

```
morning_agent/
  config.py            env-var loading + helpers
  main.py              orchestrator: build digest, send, save state
  store.py             JSON no-repeat history (30-day rolling window)
  delivery/notify.py   ntfy.sh push
  sections/
    quote.py  song.py  news.py  portfolio.py  suggestions.py
data/
  quotes.json  songs.json            curated content
  sector_peers.json  watchlist.json  stock-screen universe
  history.json                       no-repeat state (committed by CI)
.github/workflows/briefing.yml       daily schedule
.env.example                         copy to .env for local runs
requirements.txt
```

---

## 1. Local setup

```bash
git clone <this-repo> && cd Morning--Agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in values (see below)
python -m morning_agent.main
```

`.env` is gitignored. The script reads it automatically via `python-dotenv`.

---

## 2. Getting each credential

### ntfy.sh (notifications) — required
1. Pick a **hard-to-guess topic name**, e.g. `morning-briefing-7f3k9q2x`.
   The topic is effectively a password — anyone who knows it can read/post.
2. Install the **ntfy** app on iPhone *and* iPad (App Store, free).
3. In the app: **+ → Subscribe to topic →** enter the same topic name.
4. Set `NTFY_TOPIC` to that name. (No account or API key needed.)

> Self-hosting? Set `NTFY_SERVER` to your server URL; otherwise it defaults to
> the public `https://ntfy.sh`.

### GNews (news) — required for the news section
1. Sign up free at <https://gnews.io>.
2. Copy your API key from the dashboard.
3. Set `GNEWS_API_KEY`. Free tier = 100 requests/day; we use one per category
   (default 3/day). Tune categories with `NEWS_CATEGORIES`
   (e.g. `world,business,technology,science`).

### Quotes — no key needed
Uses the free [Quotable](https://github.com/lukePeavey/quotable) API and falls
back to `data/quotes.json` automatically.

### Robinhood (portfolio) — optional, fragile
> ⚠️ `robin_stocks` is **unofficial and unsupported**. It can break at any time,
> and logins from datacenter IPs (like GitHub's) are often challenged or blocked.
> If it fails, the digest still sends with a "couldn't fetch portfolio" note.

1. Set `RH_USERNAME` and `RH_PASSWORD`.
2. **Strongly recommended:** enable **authenticator-app 2FA** in the Robinhood
   app (Account → Security → Two-Factor → Authenticator App). When it shows the
   QR code, choose "**Can't scan?**" to reveal the **base32 secret** and set it
   as `RH_MFA_SECRET`. The agent uses `pyotp` to generate the 6-digit code
   headlessly, so no manual entry is needed.
   - Without this secret, a login that demands a code can't complete on a
     headless runner — the section just degrades gracefully.
3. If GitHub's IP gets persistently blocked, see
   [Running the portfolio from your phone](#optional-run-the-portfolio-from-your-phone).

### Stock ideas — no key needed
Uses [`yfinance`](https://github.com/ranaroussi/yfinance) for free price data.
Edit `data/sector_peers.json` / `data/watchlist.json` to customize the universe.

---

## 3. Deploy on GitHub Actions (free)

**Why GitHub Actions?** Truly free, no server to keep awake (unlike Render/Railway
free tiers, which sleep), built-in cron + encrypted secrets, and it can commit
the no-repeat state back to the repo.

### Add your secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add each value you're using:

| Secret | Required | Notes |
|---|---|---|
| `NTFY_TOPIC` | ✅ | your ntfy topic |
| `NTFY_SERVER` | optional | only if self-hosting ntfy |
| `GNEWS_API_KEY` | for news | from gnews.io |
| `NEWS_CATEGORIES` | optional | default `world,business,technology` |
| `NEWS_COUNTRY` / `NEWS_LANG` | optional | default `us` / `en` |
| `RH_USERNAME` / `RH_PASSWORD` | for portfolio | |
| `RH_MFA_SECRET` | for portfolio | TOTP base32 secret |
| `TIMEZONE` | optional | default `America/New_York` |

### Schedule
`.github/workflows/briefing.yml` already runs daily. Cron in GitHub Actions is
**UTC and has no DST awareness**, so it fires at both 11:00 and 12:00 UTC and a
guard step proceeds only when your local time is the **7 AM hour** — so you get
exactly one 7 AM delivery year-round. To change the time, edit the two `cron:`
lines (keep them one hour apart) and the `hour == 7` check.

### Test it now
Repo → **Actions → Morning Briefing → Run workflow**. `workflow_dispatch` bypasses
the time guard so it sends immediately. Check your phone, and the run logs for
any per-section notes.

> **Note:** GitHub disables scheduled workflows after 60 days of no repo
> activity. The daily state commit keeps the repo active, so this won't trigger
> in normal use.

---

## 4. iOS Shortcuts setup

With the **ntfy app** subscribed (step 2 above), you already receive the push
directly — no Shortcut required. Use Shortcuts only if you want extra automation:

### A) Simplest — just receive the push (recommended)
Install ntfy on iPhone + iPad, subscribe to your topic, done. The notification
arrives on both devices each morning.

### B) Mirror the briefing into a Shortcut (optional)
If you'd rather open a Shortcut to read/save the latest briefing:
1. **Shortcuts app → + (new shortcut).**
2. Add **Get Contents of URL**
   - URL: `https://ntfy.sh/<your-topic>/json?poll=1&since=12h`
   - Method: GET
3. Add **Get Dictionary from Input** (parses the JSON lines), then **Get
   Dictionary Value** → key `message` to extract the text.
4. Add **Show Result** (or **Quick Look** / **Show Notification**).
5. **Automation** tab → **+ → Time of Day → 7:05 AM → Daily → Run Immediately**,
   then run this shortcut. (Personal automations can run without prompting.)

### C) <a id="optional-run-the-portfolio-from-your-phone"></a>Optional: run the portfolio from your phone (trusted IP)
If Robinhood blocks GitHub's IP, fetch the portfolio from your phone instead:
- Keep sections 1–3 + 5 on GitHub Actions.
- Build a separate Shortcut that hits Robinhood (or a tiny endpoint you run) from
  your home network and appends the result. This avoids datacenter-IP blocks.
- This is an advanced, optional path; the cloud version degrades gracefully
  without it.

---

## Reliability & design notes

- **Always sends.** Every section runs in its own `try/except`. A failure becomes
  a visible `⚠️ Couldn't fetch X` line and the digest still goes out, with a
  footer listing what failed. The only thing that fails the run is delivery
  itself (correct — there'd be nothing to send).
- **No-repeat.** Quotes and songs are tracked by a stable hash for 30 days in
  `data/history.json`, committed back by the Action.
- **Transparent ideas.** Stock suggestions use plain momentum math (5-day change,
  vs 20-day SMA) within sectors you already hold. The rationale shows the actual
  numbers — no predictions, no black box.
- **Secrets.** Everything is via env vars / GitHub Secrets. `.env` is gitignored;
  `.env.example` is the template. Nothing is hardcoded.

## Disclaimer
The "Ideas" section is **not financial advice** and is for informational purposes
only. Robinhood integration is unofficial and may break without notice.
