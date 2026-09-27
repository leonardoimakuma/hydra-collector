# hydra-collector

Free, always-on collector for in-play soccer odds, boxscore stats and key match events from ESPN's
public (undocumented, no API key) endpoints, plus a daily pull of football-data.co.uk's closing-line
CSVs. Runs entirely on **GitHub Actions**, at **$0/month**, with no server to maintain.

## Fastest start (Windows)

Double-click `START.bat` in this folder. It installs Git and GitHub CLI if missing (run it again after an install),
logs you in to GitHub once in the browser, creates the **public** repo `hydra-collector`, pushes this folder and
triggers the first runs. The workflows already declare `permissions: contents: write`, so no repo settings change is needed.

## What it collects

Polling `https://site.web.api.espn.com/apis/site/v2/sports/soccer/...`:

- **`live_odds`** — one DraftKings moneyline / over-under / spread snapshot per event per poll tick.
- **`live_stats`** — per-team boxscore stats (shots, shots on target, possession%, corners, fouls,
  yellow/red cards, saves, offsides, plus everything else ESPN's boxscore sends — logged and kept
  verbatim in a raw JSON column), pulled at most every 120s per live event.
- **`key_events`** — goals, red cards, penalties and substitutions, with minute, period, team and
  player, deduplicated on ESPN's own event id.
- **`closing_odds`** — the last odds snapshot captured while a match was still pre-match, saved the
  moment it kicks off, so you get a clean closing line distinct from in-play noise.

Default league coverage (`hydra_scout/collect/live_odds_logger.py:DEFAULT_SLUGS`): English Premier
League + Championship, La Liga, Bundesliga, Serie A, Ligue 1, Brasileirão Séries A/B, UEFA Champions/
Europa/Conference League, MLS, Liga MX, Argentine Liga Profesional, Primeira Liga, Eredivisie — plus
ESPN's own `all` slug, polled every tick, which covers ~100 events/day across every competition ESPN
carries (the named slugs are a periodic sweep so a busy domestic day isn't clipped by `all`'s page size).

- **`lineups`** — confirmed starting XIs + bench (team, player, ESPN athlete id, position, starter flag,
  jersey, formation) from `/summary`'s `rosters`, stored once per event: every pre-match event kicking
  off within 75 min is polled every 5 min until ESPN publishes the teams (~60 min before kickoff).

`hydra_scout/collect/export.py` flattens the sqlite tables into gzipped daily CSVs under
`data/live/<YYYY-MM-DD>/{odds,stats,events,closing,lineups}.csv.gz`.

### Player props

Bookmaker prop odds are archived in a separate PRIVATE repo (`hydra-props`), because the odds provider's terms forbid redistributing its data. This public repo only holds ESPN-derived live data and football-data CSVs.

## Cost: $0

- GitHub Actions is **free and unmetered on standard runners for public repositories** — this only
  works because the repo is public. (A private repo gets 2,000 free minutes/month on GitHub Free,
  which this workflow would burn through in a few days.)
- No server, no paid API, no keys: ESPN's public scoreboard/summary JSON and football-data CSVs only.

## Set this up (one time)

1. **Create a new public repo** on GitHub, e.g. `hydra-collector`.
2. **Push this folder's contents** to it as the repo root:
   ```
   cd collector_repo
   git init
   git add -A
   git commit -m "Initial hydra-collector"
   git branch -M main
   git remote add origin https://github.com/<you>/hydra-collector.git
   git push -u origin main
   ```
3. **Enable Actions** on the new repo if it isn't already (Settings → Actions → General → "Allow all
   actions and reusable workflows"), and check Settings → Actions → General → Workflow permissions is
   set to "Read and write permissions" (needed for the workflow to commit `data/**` back to the repo;
   the workflow also declares `permissions: contents: write` itself, but the repo-level setting must
   allow it).
4. That's it — `collect.yml` fires at 00:00/06:00/12:00/18:00 UTC and `daily.yml` at 05:00 UTC. You
   can also trigger either manually from the Actions tab ("Run workflow") to test immediately rather
   than waiting for the next scheduled slot.

Data shows up under `data/live/<day>/` and `data/fd/` in the repo itself, committed by the workflow's
own bot identity — pull the repo (or read the raw CSV URLs) to get it into the rest of Hydra.

## Limits to know about

- **The collected odds data is public.** Anyone can see this repo, its Actions logs, and every CSV it
  commits. Do not push anything else (models, strategy code, credentials) into this repo — keep it
  scoped to raw ESPN/football-data collection only. Hydra's models and strategy logic stay in the
  private `hydra_scout` repo.
- **Cron can be delayed.** GitHub's scheduled triggers are best-effort and can slip by several
  minutes (worse when GitHub Actions is under heavy global load) — don't assume a run starts exactly
  on the hour. The 5h50m run length plus 355-minute job timeout leaves slack for this.
- **6-hour hard cap per job.** GitHub kills any job after 6 hours regardless of `timeout-minutes`;
  the collector stops itself at 5h50m so export + commit + push always have time to run first.
- **ESPN's `odds` array is DraftKings-only** — no multi-book comparison from this endpoint alone.
- Each live event's boxscore/key-events pull is throttled to once per 120s
  (`--stats-interval`) — a stat or event that appears and reverts faster than that can be missed.

## Running it yourself (without GitHub Actions)

```
pip install -r requirements.txt
python -m hydra_scout.collect.live_odds_logger --interval 60 --minutes 90
python -m hydra_scout.collect.export --day 2026-09-27
```

`python -m hydra_scout.collect.live_odds_logger --help` and `... export --help` list every flag.

## Alternative: run on Hermes (systemd) instead of GitHub Actions

If you'd rather run this continuously on the Hermes VPS instead of (or alongside) GitHub Actions:

```ini
# /etc/systemd/system/hydra-collector.service
[Unit]
Description=Hydra ESPN in-play odds/stats/events collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hydra
WorkingDirectory=/opt/hydra-collector
ExecStart=/opt/hydra-collector/.venv/bin/python -m hydra_scout.collect.live_odds_logger --interval 60
Restart=always
RestartSec=15
Environment=HYDRA_HOME=/opt/hydra-collector/.hydra_scout

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/hydra-collector-export.timer
[Unit]
Description=Run hydra-collector export daily

[Timer]
OnCalendar=*-*-* 00:05:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/hydra-collector-export.service
[Unit]
Description=Export hydra-collector sqlite to daily CSVs

[Service]
Type=oneshot
User=hydra
WorkingDirectory=/opt/hydra-collector
Environment=HYDRA_HOME=/opt/hydra-collector/.hydra_scout
ExecStart=/opt/hydra-collector/.venv/bin/python -m hydra_scout.collect.export --out-dir /opt/hydra-collector/data/live
```

```
systemctl daemon-reload
systemctl enable --now hydra-collector.service hydra-collector-export.timer
```

No 6-hour cap, no cron drift, no public-repo constraint — but it depends on Hermes staying up, and
someone has to maintain the box. The `Restart=always` + sqlite's own dedup-on-unchanged-row logic
make a mid-run restart harmless either way.
