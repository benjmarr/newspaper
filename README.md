# The Morning Edition: a personal daily newspaper

A self-hosted, tablet-first daily newspaper. Every morning it gathers your RSS/Atom feeds, removes duplicates and the topics you never want to see, ranks what's left for you, and lays it out as a printed-style paper. The paper also carries local weather, official Bureau of Meteorology warnings and an "On this day" section.

It runs as one Docker container: Python/FastAPI, server-rendered pages, SQLite in a `/data` volume. It has no external database and no JavaScript build step.

---

## Contents

1. [Deployment](#1-deployment)
2. [Using the app](#2-using-the-app)
3. [Configuration](#3-configuration)
4. [Updating](#4-updating)
5. [Backup and restore](#5-backup-and-restore)
6. [Security basics](#6-security-basics)
7. [Troubleshooting](#7-troubleshooting)
8. [How it works](#8-how-it-works)
9. [Development and tests](#9-development-and-tests)

---

## 1. Deployment

You need a Linux machine or VM with Docker Engine and the Docker Compose plugin. On Proxmox, use a small Debian/Ubuntu VM, or an LXC container with nesting enabled. 1 CPU, 512 MB RAM and 2 GB disk is plenty.

1. Copy this project to the server, for example into `/opt/newspaper`:

   ```bash
   git clone https://github.com/benjmarr/newspaper.git /opt/newspaper
   ```

2. Create your settings file and set a password:

   ```bash
   cd /opt/newspaper
   cp .env.example .env
   nano .env
   ```

   At minimum, change `APP_PASSWORD`.

3. Build and start:

   ```bash
   docker compose up -d --build
   ```

4. Check it is healthy (it takes about 20 seconds):

   ```bash
   docker compose ps
   ```

5. On your tablet, open `http://<server-ip>:8080` (or the `PAPER_PORT` you chose) and sign in. If today's build time has already passed, the first edition builds straight away and the page refreshes when it's ready.

6. Optional: add it to the home screen. In iPad Safari, tap Share, then **Add to Home Screen**. In Android Chrome, open the menu and tap **Add to Home screen**.

---

## 2. Using the app

**The paper.** The masthead shows the edition's date. Use **‹ Earlier** and **Later ›** to move between editions, or **Archive** to see them all grouped by month. The front page leads with the three strongest stories that aren't opinion pieces, with the weather beside them. After that comes one section per topic and then "On this day".

**Story cards.** Every card shows a label, the source, the publication time, a short summary and a **Read ↗** link to the original, which opens in a new tab. When several outlets ran the same story, you get one card that says "Also reported by …".

**Labels (Reporting / Analysis / Opinion) are best guesses.** They come from feed categories, web addresses (such as `/opinion/` or `/commentisfree/`) and headline prefixes (such as "Analysis:"). If one is wrong, tap **Label** on the card and pick the right one. To label every story from a source the same way, set **Treat as** for that source in Settings → Sources.

**Interested / Not interested.** These buttons teach the paper what you like:

- **Interested** boosts the words in that headline and the story's source.
- **Not interested** lowers them and hides the story.
- Tap the same button again to undo.
- Everything learned fades by 5% with each new edition, so old taps matter less over time.
- To see, forget or reset what has been learned, go to Settings → Learning.

**Wildcard.** Each section gets one extra story from further down the rankings, marked *Wildcard*, so you don't only ever see more of the same.

**Rebuild now.** This button is at the bottom of the latest edition and in Settings. Use it after changing topics, sources or exclusions. A banner shows while the build runs, and the page refreshes when it's done. Rebuilding replaces today's edition. Stories that appeared in *earlier* editions are never repeated.

**Weather.** The box shows:

- current conditions, the high and low, the chance of rain and rainfall in mm, wind and gusts, the UV index, and sunrise and sunset
- a five-day outlook
- heads-up chips worked out from the forecast numbers: heat, frost, strong wind, wet day, high UV and thunderstorms. These are labelled "From the forecast" because they are not official warnings.
- official warnings from the Bureau of Meteorology, shown separately and credited to the BoM

If the forecast can't be fetched, the last saved forecast is shown, marked "as of <time>". If the warnings can't be fetched, the box says so instead of showing nothing.

**Settings tabs:**

| Tab | What you can do |
|---|---|
| General | Daily build time, how old stories may be, weather location (search by place name), sign out |
| Topics | Add, rename or reorder sections. Set keywords, importance (1–5) and story count, or hide a section |
| Sources | Add a feed (it's checked first), then pause, resume, test, remove or re-assign it. Set its priority and "treat as" label. Shows each feed's status and last error |
| Exclusions | Words and phrases that remove a story completely. Comes pre-loaded with celebrity-gossip terms |
| Learning | The words and sources you've shown you like or avoid. Forget any of them, or reset everything |
| Backups & system | Last build statistics, next build time, **Back up now**, download backups |

---

## 3. Configuration

These settings live in `.env`. Everything else is changed in the app.

| Variable | Default | Meaning |
|---|---|---|
| `APP_PASSWORD` | *(required)* | Password for the web page. The app refuses to start without it. Changing it signs out every device. |
| `TZ` | `Australia/Melbourne` | Time zone for the build schedule and every time shown |
| `PAPER_PORT` | `8080` | Port on the server where the paper is served |
| `PAPER_NAME` | `The Morning Edition` | Masthead name |
| `BACKUP_KEEP` | `14` | Number of nightly backups to keep |
| `ALERT_FEED_URL` | BoM Victoria warnings | RSS feed for official warnings. For other states, use the matching BoM `IDZ000xx` feed |

After editing `.env`, run:

```bash
docker compose up -d
```

**Schedule.** These times come from `TZ`:

- The edition is built daily at **05:00**. You can change this in Settings → General.
- The weather refreshes every 30 minutes.
- The database is backed up nightly at **02:30**.
- If the server was off at build time, the edition is built as soon as the app starts again.

**Seeded sources.** These are added on first start:

- ABC News, Guardian Australia and SBS News (Local & Australia)
- BBC World, Guardian World and Al Jazeera (World)
- Ars Technica, The Verge and BBC Technology (Technology)
- Motorsport.com and Speedcafe (Motorsport)

The Warrnambool Standard isn't included because no verified feed was found. If you find one, add it under Sources. The Local & Australia topic already has Warrnambool and south-west Victoria keywords, so local stories from the national feeds rise to the top.

---

## 4. Updating

```bash
cd /opt/newspaper
git pull
docker compose up -d --build
```

Your data lives in the Docker volume `newspaper-data`, so rebuilding the container doesn't touch it. To be extra safe, tap **Back up now** first.

---

## 5. Backup and restore

**Automatic backups.** Every night at 02:30 the app writes a consistent copy of the database to `/data/backups/paper-YYYYMMDD-HHMMSS.db` and keeps the newest `BACKUP_KEEP`. Settings → Backups & system lists them, has a **Back up now** button and lets you **Download** any of them to keep elsewhere.

**Copy backups off the server** (recommended, for example from a nightly host cron job):

```bash
docker compose cp paper:/data/backups ./paper-backups
```

**Restore** (the app must be stopped):

1. Stop the app:

   ```bash
   docker compose stop
   ```

2. List the available backups:

   ```bash
   docker compose run --rm paper python -m app.restore list
   ```

3. Restore one of them:

   ```bash
   docker compose run --rm paper python -m app.restore paper-20261112-023000.db
   ```

4. Start the app again:

   ```bash
   docker compose start
   ```

The restore command checks the backup's integrity first, and saves your current database as a new backup before replacing it. That means a restore can itself be undone.

**To restore a file you downloaded earlier,** copy it into the backups folder first, then use the steps above:

```bash
docker compose cp ./paper-20261101-023000.db paper:/data/backups/
```

**Moving to a new server.** Download a backup, deploy on the new server, copy the backup in and restore it as above.

---

## 6. Security basics

This app is built for a trusted home LAN, over plain HTTP.

- **Don't expose it to the internet.** Don't port-forward it. For access away from home, use a VPN such as WireGuard or Tailscale.
- **Password.** Use a long `APP_PASSWORD`.
- **Login protection.** Five wrong passwords lock that device out for five minutes.
- **Sessions.** A signed, HttpOnly cookie keeps you signed in for 30 days. The signing key lives in `/data/secret_key`. Changing `APP_PASSWORD` signs everyone out.
- **Feed content.** All feed content is treated as untrusted:
  - HTML is stripped and everything is escaped.
  - Only `http(s)` links are kept.
  - Outbound links use `rel="noopener noreferrer"`.
  - A strict Content-Security-Policy allows only the app's own scripts and styles.
- **Cross-site requests.** Form posts from other websites are refused (Origin check plus SameSite cookies).
- **Container hardening:**
  - runs as a non-root user (uid 10001)
  - read-only root filesystem; only `/data` and a small `/tmp` are writable
  - all Linux capabilities dropped
  - `no-new-privileges`
  - memory and process limits
  - log rotation
  - a health check
- **Plain HTTP.** Anyone on your LAN who can capture traffic could see the session cookie. That's fine for most home networks. If you want HTTPS, put a reverse proxy such as Caddy or Nginx Proxy Manager in front. That also enables offline mode (see below).

---

## 7. Troubleshooting

**It won't start, and the logs say "APP_PASSWORD is not set".** Edit `.env`, set `APP_PASSWORD`, then run `docker compose up -d`.

**To see the logs:**

```bash
docker compose logs -f
```

**The edition is empty, or "Offline edition" is shown.** The server couldn't reach any feeds, so the paper was built from stories saved earlier. Check the server's internet access and DNS, then open Settings → Sources: each feed shows its last error. Tap **Rebuild now** once the connection is back.

**One source shows "Failing".** Open Settings → Sources and tap **Test now** to see the exact error. `HTTP 404` usually means the feed moved; find the new address, remove the old source and add the new one. `HTTP 403` means the site blocks automated readers. A failing feed never stops the rest of the edition.

**Weather says "as of …".** Open-Meteo couldn't be reached on the last attempt, so the saved forecast is shown. It retries every 30 minutes. Open-Meteo's free tier has a daily request limit per IP address. The app uses about 50 requests a day, well under the limit, unless many devices share your public IP.

**Warnings say "Couldn't fetch warnings".** The BoM feed was unreachable. Check bom.gov.au directly. The BoM sometimes blocks requests it thinks are automated; the app identifies itself politely and retries every 30 minutes.

**The build time didn't change.** Saving in Settings → General reschedules the build immediately. Confirm it under "Next build". Times use `TZ` from `.env`.

**Offline reading doesn't work on the tablet.** Browsers only allow the service worker that saves the last edition on **HTTPS** or `localhost`. Over plain `http://192.168.x.x` it is switched off, and the paper still works fine while you're online. To enable offline reading, do one of these:

- Put the app behind an HTTPS reverse proxy (Caddy with its internal CA, or Nginx Proxy Manager), and trust that certificate on the tablet.
- On Android Chrome only: open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, add `http://<server-ip>:8080`, enable it and relaunch Chrome.

For the same reason, "Add to Home Screen" over plain HTTP gives a home-screen shortcut. On iPad it opens full-screen; on Android it may open in a browser tab.

**Locked out after typing the wrong password.** Wait five minutes. Restarting the container also clears the lockout.

**"Cross-site request refused".** You opened the app through a different hostname or port than the one in the browser's address bar, for example through a reverse proxy that rewrites the Host header. Make the proxy pass the original `Host` header.

**Permission errors on /data.** Use the named volume in the provided `docker-compose.yml`. If you switch to a bind mount such as `./data:/data`, first run:

```bash
sudo chown -R 10001:10001 ./data
```

**Start completely fresh** (this deletes everything, including backups):

```bash
docker compose down -v
```

---

## 8. How it works

- **Fetching.** All active feeds are fetched in parallel, eight at a time:
  - Each request has a 15 s timeout and up to 2 retries. Retries only happen for timeouts and 5xx/429 responses.
  - The app sends conditional requests (ETag / Last-Modified).
  - Each feed's status and last error are recorded.
- **Filtering.** Exclusion terms are matched as whole words and case-insensitively against the headline, summary and categories. Stories you marked *Not interested*, and stories already printed in an earlier edition, are removed. Repeats are caught by ID and by fuzzy headline matching over the last 10 days.
- **Duplicates.** Web addresses are normalised: no `www.`, tracking parameters, fragments or trailing slashes. Headlines are fuzzy-matched by word overlap plus character similarity. Matches merge into one card, led by the highest-priority source.
- **Ranking.** Each story gets a score from:
  - topic importance
  - keyword matches (a match in the headline counts double)
  - source priority
  - learned word and source weights
  - freshness (half-life of 12 hours)
  - how many outlets covered it
- **Selection:**
  - The top 3 non-opinion stories lead the front page.
  - Each section then gets its story count, with at most 2 opinion pieces, plus one wildcard.
- **On this day.** Uses Wikipedia's selected anniversaries for the date, topped up from the full events list for centuries the selected list misses. Six items are spread across history.
- **Clean-up.** Stories older than 30 days that never appeared in an edition are deleted, so the database stays small.

---

## 9. Development and tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests use mock feeds, a mock weather API and a mock Wikipedia, so they never touch the network. They cover:

- login, sessions and lockout
- exclusions
- URL and headline de-duplication
- label detection and corrections
- feedback learning, decay and undo
- failing, timed-out, broken and paused sources
- offline builds
- never repeating stories across editions
- topics, sources, build time and location settings
- weather fallbacks and warnings
- backups and their download
- HTML escaping

To run locally without Docker:

```bash
APP_PASSWORD=dev DATA_DIR=./data .venv/bin/uvicorn app.main:app_factory --factory --port 8080
```
