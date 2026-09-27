"""
Polls ESPN's free soccer scoreboard (and, per live event, the summary endpoint) for DraftKings odds,
per-team boxscore stats and key match events, logging a row per snapshot to sqlite so we can see
whether in-play odds actually move (and how) and build an in-play training set, rather than trusting
the football-data.co.uk closing line as the last word.

Endpoint (undocumented, no key): https://site.web.api.espn.com/apis/site/v2/sports/soccer/<slug>/scoreboard
  - <slug> = "all" aggregates today's events across every competition ESPN carries (confirmed:
    ~100 events/day, international-window included) in one call; a competition slug like "eng.1"
    scopes to one league and, unlike "all", also names itself in `leagues[0]` (id/slug/name) —
    /all's events carry no league field at all.
  - Each event's `competitions[0]["odds"][0]` is one bookmaker (DraftKings, "provider.name"). Every
    market (moneyline / total / pointSpread) nests "open" (pre-match), "close" (line at the
    scheduled close) and, ONLY once a match has kicked off, "current" — a genuinely live-updating
    quote (confirmed empirically 2026-09-26: an in-play match's total line moved from o2.5 to o1.5
    and its draw moneyline from +230 to +195 between the pre-match close and an in-play poll). We
    always take current > close > open, so pre-match snapshots use the close/open line and in-play
    ones pick up whatever ESPN has just updated.
  - /<slug>/summary?event=<id> carries: the same odds shape under "pickcenter" (used as a fallback
    when a live event's scoreboard entry has no odds at all); "boxscore.teams[].statistics", a named
    stat list (shots, shots on target, possession%, corners, fouls, cards, saves, offsides, plus
    whatever else ESPN sends — logged once per run and preserved raw per row); and "keyEvents", a
    chronological list of goals/cards/penalties/substitutions/etc. with minute, team and player.

Three sqlite tables, `live_odds`'s schema untouched for backward compatibility:
  - `live_odds` (unchanged): one DraftKings snapshot per event per poll tick.
  - `live_stats`: one boxscore-stat snapshot per team per event, pulled from /summary at most every
    `--stats-interval` seconds (default 120s) per live event.
  - `key_events`: goals / red cards / penalties / substitutions from the same /summary call,
    deduplicated on ESPN's own event id (`UNIQUE(event_id, espn_event_id)`, `INSERT OR IGNORE`).
  - `closing_odds`: the last `live_odds` row captured while an event was still `pre` (or the current
    one, if none was ever captured), copied over the moment its state first flips to `in` — one row
    per event (`UNIQUE(event_id)`), giving a clean closing-line benchmark distinct from in-play noise.

Robustness: every HTTP call retries with backoff and returns None (never raises) on failure — 429s
and 5xx back off and retry (honoring `Retry-After` when present), other 4xx responses (e.g. a summary
call for an event ESPN has already dropped) are treated as permanent and not retried; the poll loop
wraps each tick in try/except so one bad response never kills a many-hour run; every parsed field is
independently null-tolerant, so a partial ESPN response never raises. A heartbeat line is logged
every 10 minutes so an unattended multi-hour run is observable from its log alone. `--until` (a UTC
timestamp) or SIGTERM/SIGINT (e.g. a CI job timeout) both exit the poll loop cleanly, commit, and
return 0.

Politeness: exactly one GET to /all per --interval; the optional extra league slugs (mainly useful
on a normal, non-international-break day, when a single /all response can already be near ESPN's
~100-event page and might not include every domestic league) are swept only once every
`--slug-sweep-secs` (default 300s); each live event gets at most one /summary call per
`--stats-interval` (default 120s), serving the odds fallback, live_stats and key_events alike so
that one throttle governs all three rather than issuing separate calls to the same endpoint.

CLI:
  python -m hydra_scout.collect.live_odds_logger --interval 60 --minutes 90
  python -m hydra_scout.collect.live_odds_logger --interval 60 --until 2026-09-27T06:00:00Z
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone

import httpx

BASE = "https://site.web.api.espn.com/apis/site/v2/sports/soccer"
DEFAULT_SLUGS = ("eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "bra.1", "bra.2", "uefa.champions",
                  "uefa.europa", "uefa.europa.conf", "usa.1", "mex.1", "arg.1", "por.1", "ned.1", "eng.2")

FIELDS = ["ts_utc", "event_id", "league_slug", "league_name", "home", "away", "state", "period", "clock",
          "score_home", "score_away", "provider", "ml_home", "ml_draw", "ml_away", "ml_home_dec", "ml_draw_dec",
          "ml_away_dec", "ou_line", "over_odds", "under_odds", "spread", "spread_odds", "kickoff_utc"]
_INT_FIELDS = {"period", "score_home", "score_away", "ml_home", "ml_draw", "ml_away", "over_odds", "under_odds", "spread_odds"}
_REAL_FIELDS = {"ml_home_dec", "ml_draw_dec", "ml_away_dec", "ou_line", "spread"}
# comparison scope for the "skip if nothing changed but ts" rule — everything but the timestamp
_CMP_FIELDS = [f for f in FIELDS if f != "ts_utc"]


# --------------------------------------------------------------------------- #
# odds-tree parsing (pure functions, no I/O — this is what the tests exercise)
# --------------------------------------------------------------------------- #

def american_to_decimal(odds: int | None) -> float | None:
    if odds is None:
        return None
    return round(1 + odds / 100, 4) if odds > 0 else round(1 + 100 / abs(odds), 4)


def _to_int(s) -> int | None:
    """Parses both American-odds strings ('+170', '-225') and plain score strings ('0', '2')."""
    if s in (None, ""):
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _to_float(s) -> float | None:
    """Parses a boxscore stat displayValue ('40.9', '5') that may be int- or float-shaped."""
    if s in (None, ""):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _pick(side: dict | None) -> dict | None:
    """The freshest snapshot for one selection: live 'current' if the match has kicked off,
    else the pre-match 'close', else 'open'."""
    if not side:
        return None
    return side.get("current") or side.get("close") or side.get("open")


def _tags(snapshot: dict | None) -> dict:
    return (((snapshot or {}).get("link") or {}).get("tracking") or {}).get("tags") or {}


def moneyline(odds0: dict) -> tuple[int | None, int | None, int | None]:
    ml = odds0.get("moneyline") or {}
    home, away, draw = _pick(ml.get("home")), _pick(ml.get("away")), _pick(ml.get("draw"))
    mh, ma, md = _to_int((home or {}).get("odds")), _to_int((away or {}).get("odds")), _to_int((draw or {}).get("odds"))
    if md is None:
        md = _to_int((odds0.get("drawOdds") or {}).get("moneyLine"))
    return mh, md, ma


def total_ou(odds0: dict) -> tuple[float | None, int | None, int | None]:
    t = odds0.get("total") or {}
    over, under = _pick(t.get("over")), _pick(t.get("under"))
    line = None
    for snap in (over, under):
        raw = (snap or {}).get("line")
        if raw:
            try:
                line = float(str(raw).lstrip("ou"))
                break
            except ValueError:
                pass
    if line is None:
        line = odds0.get("overUnder")
    return line, _to_int((over or {}).get("odds")), _to_int((under or {}).get("odds"))


def point_spread(odds0: dict) -> tuple[float | None, int | None]:
    home = _pick((odds0.get("pointSpread") or {}).get("home"))
    line = None
    if home and home.get("line") not in (None, ""):
        try:
            line = float(home["line"])
        except ValueError:
            line = None
    return line, _to_int((home or {}).get("odds"))


def league_from_odds(odds0: dict) -> str | None:
    """Best-effort league slug when the caller has none (e.g. from /all): DraftKings' own deep
    links carry it in tracking.tags.league on any snapshot that has a link (open snapshots don't)."""
    ml = odds0.get("moneyline") or {}
    for side_key in ("home", "away", "draw"):
        side = ml.get(side_key) or {}
        for snap_key in ("current", "close"):
            lg = _tags(side.get(snap_key)).get("league")
            if lg:
                return lg
    return None


def extract_row(event: dict, league_slug: str | None = None, league_name: str | None = None) -> dict | None:
    """Map one ESPN scoreboard event to a flat snapshot dict (ts_utc/event_id not yet set)."""
    comps = event.get("competitions") or [{}]
    comp = comps[0]
    status = comp.get("status") or {}
    state = ((status.get("type") or {}) or {}).get("state")
    competitors = comp.get("competitors") or []
    home_c = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away_c = next((c for c in competitors if c.get("homeAway") == "away"), {})
    odds_list = comp.get("odds") or []
    odds0 = (odds_list[0] if odds_list else None) or {}   # ESPN sometimes sends odds:[None]
    mh, md, ma = moneyline(odds0)
    ou_line, over_odds, under_odds = total_ou(odds0)
    spread, spread_odds = point_spread(odds0)
    return {
        "event_id": event.get("id"),
        "league_slug": league_slug or league_from_odds(odds0),
        "league_name": league_name or comp.get("altGameNote") or event.get("name"),
        "home": (home_c.get("team") or {}).get("displayName"),
        "away": (away_c.get("team") or {}).get("displayName"),
        "state": state,
        "period": status.get("period"),
        "clock": status.get("displayClock"),
        "score_home": _to_int(home_c.get("score")),
        "score_away": _to_int(away_c.get("score")),
        "provider": (odds0.get("provider") or {}).get("name"),
        "ml_home": mh, "ml_draw": md, "ml_away": ma,
        "ml_home_dec": american_to_decimal(mh), "ml_draw_dec": american_to_decimal(md), "ml_away_dec": american_to_decimal(ma),
        "ou_line": ou_line, "over_odds": over_odds, "under_odds": under_odds,
        "spread": spread, "spread_odds": spread_odds,
        "kickoff_utc": event.get("date"),
    }


def within_window(event: dict, hours: float = 3.0, now: datetime | None = None) -> bool:
    """True for a live match, or a not-yet-started one kicking off within `hours` (small negative
    grace so an event that just kicked off but hasn't flipped to 'in' yet is still picked up)."""
    comp = (event.get("competitions") or [{}])[0]
    state = ((comp.get("status") or {}).get("type") or {}).get("state")
    if state == "in":
        return True
    if state != "pre":
        return False
    now = now or datetime.now(timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            kickoff = datetime.strptime(event.get("date", ""), fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            kickoff = None
    if kickoff is None:
        return False
    delta = (kickoff - now).total_seconds()
    return -1800 <= delta <= hours * 3600


# --------------------------------------------------------------------------- #
# boxscore stats + key events (pure functions, no I/O — parsed from /summary)
# --------------------------------------------------------------------------- #

# ESPN boxscore stat "name" -> our column name, for the subset explicitly requested. Every stat
# ESPN actually sends (this set and anything else) is preserved verbatim in raw_stats_json.
STAT_NAME_MAP = {
    "totalShots": "shots",
    "shotsOnTarget": "shots_on_target",
    "possessionPct": "possession_pct",
    "wonCorners": "corners",
    "foulsCommitted": "fouls",
    "yellowCards": "yellow_cards",
    "redCards": "red_cards",
    "saves": "saves",
    "offsides": "offsides",
}
STATS_NAMED_FIELDS = ["shots", "shots_on_target", "possession_pct", "corners", "fouls",
                       "yellow_cards", "red_cards", "saves", "offsides"]
STATS_FIELDS = (["ts_utc", "event_id", "league_slug", "team_id", "team_side", "team_name", "state",
                  "period", "clock", "score_home", "score_away"] + STATS_NAMED_FIELDS + ["raw_stats_json"])
STATS_INT_FIELDS = {"period", "score_home", "score_away", "shots", "shots_on_target", "corners",
                     "fouls", "yellow_cards", "red_cards", "saves", "offsides"}
STATS_REAL_FIELDS = {"possession_pct"}
_STATS_CMP_FIELDS = [f for f in STATS_FIELDS if f != "ts_utc"]

EVENT_FIELDS = ["ts_utc", "event_id", "espn_event_id", "league_slug", "type", "type_slug", "minute",
                 "period", "team_id", "team_name", "player", "text", "scoring_play", "wallclock"]
EVENT_INT_FIELDS = {"period", "scoring_play"}

# substring/exact markers of summary["keyEvents"][*]["type"]["type"] we keep (goals in all their
# forms — "goal", "goal---header", "goal---volley", "own-goal" — red cards, penalties, subs); things
# like "start-delay"/"halftime"/"kickoff" are dropped.
_TRACKED_KEY_EVENT_MARKERS = ("goal", "red-card", "penalty")


def _is_tracked_key_event(type_slug: str | None) -> bool:
    if not type_slug:
        return False
    t = type_slug.lower()
    return t == "substitution" or any(marker in t for marker in _TRACKED_KEY_EVENT_MARKERS)


def parse_boxscore_stats(summary: dict | None, event_id: str, league_slug: str | None, state: str | None,
                          period: int | None, clock: str | None, score_home: int | None,
                          score_away: int | None) -> list[dict]:
    """Flattens summary['boxscore']['teams'][*]['statistics'] into one row per team. Tolerant of a
    missing/empty boxscore (returns []) and of any single stat being absent (its named column is
    None); the full raw name->displayValue set ESPN actually sent — named or not — is kept verbatim
    in raw_stats_json so nothing ESPN provides is silently dropped."""
    teams = ((summary or {}).get("boxscore") or {}).get("teams") or []
    rows = []
    for t in teams:
        if not t:
            continue
        stats_list = t.get("statistics") or []
        raw = {s.get("name"): s.get("displayValue") for s in stats_list if s and s.get("name")}
        row = {
            "event_id": event_id,
            "league_slug": league_slug,
            "team_id": (t.get("team") or {}).get("id"),
            "team_side": t.get("homeAway"),
            "team_name": (t.get("team") or {}).get("displayName"),
            "state": state,
            "period": period,
            "clock": clock,
            "score_home": score_home,
            "score_away": score_away,
            "raw_stats_json": json.dumps(raw, sort_keys=True) if raw else None,
        }
        for espn_name, field in STAT_NAME_MAP.items():
            value = raw.get(espn_name)
            row[field] = _to_float(value) if field in STATS_REAL_FIELDS else _to_int(value)
        rows.append(row)
    return rows


def parse_key_events(summary: dict | None, event_id: str, league_slug: str | None) -> list[dict]:
    """Flattens summary['keyEvents'] down to goals / red cards / penalties / substitutions, each
    tagged with minute, period, team and (when ESPN names one) the player involved. Every ESPN
    key-event id is kept verbatim as espn_event_id so callers can dedupe with INSERT OR IGNORE
    against a UNIQUE(event_id, espn_event_id) constraint — re-parsing the same summary twice (e.g.
    across polls) never inserts a duplicate row."""
    events = (summary or {}).get("keyEvents") or []
    rows = []
    for e in events:
        if not e:
            continue
        type_info = e.get("type") or {}
        if not _is_tracked_key_event(type_info.get("type")):
            continue
        team = e.get("team") or {}
        participants = e.get("participants") or []
        player = None
        if participants and participants[0]:
            player = (participants[0].get("athlete") or {}).get("displayName")
        rows.append({
            "event_id": event_id,
            "espn_event_id": e.get("id"),
            "league_slug": league_slug,
            "type": type_info.get("text"),
            "type_slug": type_info.get("type"),
            "minute": (e.get("clock") or {}).get("displayValue") or None,
            "period": (e.get("period") or {}).get("number"),
            "team_id": team.get("id"),
            "team_name": team.get("displayName"),
            "player": player,
            "text": e.get("text"),
            "scoring_play": 1 if e.get("scoringPlay") else 0,
            "wallclock": e.get("wallclock"),
        })
    return rows


# --------------------------------------------------------------------------- #
# HTTP (retry/backoff, never raises)
# --------------------------------------------------------------------------- #

def _get_json(session: httpx.Client, url: str, timeout: float = 15.0, retries: int = 3) -> dict | None:
    backoff = 2.0
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == retries - 1:
                    print(f"[live_odds_logger] GET failed ({retries}x, status {r.status_code}): {url}",
                          file=sys.stderr, flush=True)
                    return None
                wait = backoff
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        pass
                time.sleep(wait)
                backoff *= 2
                continue
            r.raise_for_status()  # other 4xx: not retried, treated as permanent
            return r.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else "?"
            print(f"[live_odds_logger] GET failed (no retry, status {status}): {url} ({e!r})",
                  file=sys.stderr, flush=True)
            return None
        except Exception as e:
            if attempt == retries - 1:
                print(f"[live_odds_logger] GET failed ({retries}x): {url} ({e!r})", file=sys.stderr, flush=True)
            else:
                time.sleep(backoff)
                backoff *= 2
    return None


# --------------------------------------------------------------------------- #
# sqlite storage
# --------------------------------------------------------------------------- #

def _coltype(f: str) -> str:
    if f in _INT_FIELDS:
        return "INTEGER"
    if f in _REAL_FIELDS:
        return "REAL"
    return "TEXT"


def ensure_table(db: sqlite3.Connection) -> None:
    cols = ", ".join(f"{f} {_coltype(f)}" for f in FIELDS)
    db.execute(f"CREATE TABLE IF NOT EXISTS live_odds (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
    db.execute("CREATE INDEX IF NOT EXISTS idx_live_odds_event ON live_odds(event_id, id)")
    db.commit()


def row_unchanged(db: sqlite3.Connection, event_id: str, row: dict) -> bool:
    """Skip-duplicate rule: nothing but ts_utc differs from the last stored snapshot of this event."""
    cur = db.execute(f"SELECT {', '.join(_CMP_FIELDS)} FROM live_odds WHERE event_id=? ORDER BY id DESC LIMIT 1", (event_id,))
    prev = cur.fetchone()
    if prev is None:
        return False
    return tuple(prev) == tuple(row.get(f) for f in _CMP_FIELDS)


def insert_row(db: sqlite3.Connection, row: dict) -> None:
    cols = ", ".join(FIELDS)
    qs = ", ".join("?" for _ in FIELDS)
    db.execute(f"INSERT INTO live_odds ({cols}) VALUES ({qs})", tuple(row.get(f) for f in FIELDS))


def ensure_stats_table(db: sqlite3.Connection) -> None:
    cols = ", ".join(f"{f} {'INTEGER' if f in STATS_INT_FIELDS else ('REAL' if f in STATS_REAL_FIELDS else 'TEXT')}"
                      for f in STATS_FIELDS)
    db.execute(f"CREATE TABLE IF NOT EXISTS live_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
    db.execute("CREATE INDEX IF NOT EXISTS idx_live_stats_event ON live_stats(event_id, team_id, id)")
    db.commit()


def stats_row_unchanged(db: sqlite3.Connection, event_id: str, team_id: str | None, row: dict) -> bool:
    cur = db.execute(
        f"SELECT {', '.join(_STATS_CMP_FIELDS)} FROM live_stats WHERE event_id=? AND team_id IS ? ORDER BY id DESC LIMIT 1",
        (event_id, team_id))
    prev = cur.fetchone()
    if prev is None:
        return False
    return tuple(prev) == tuple(row.get(f) for f in _STATS_CMP_FIELDS)


def insert_stats_row(db: sqlite3.Connection, row: dict) -> None:
    cols = ", ".join(STATS_FIELDS)
    qs = ", ".join("?" for _ in STATS_FIELDS)
    db.execute(f"INSERT INTO live_stats ({cols}) VALUES ({qs})", tuple(row.get(f) for f in STATS_FIELDS))


def ensure_key_events_table(db: sqlite3.Connection) -> None:
    cols = ", ".join(f"{f} {'INTEGER' if f in EVENT_INT_FIELDS else 'TEXT'}" for f in EVENT_FIELDS)
    db.execute(f"CREATE TABLE IF NOT EXISTS key_events (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols}, "
               f"UNIQUE(event_id, espn_event_id))")
    db.execute("CREATE INDEX IF NOT EXISTS idx_key_events_event ON key_events(event_id, id)")
    db.commit()


def insert_key_event(db: sqlite3.Connection, row: dict) -> bool:
    """INSERT OR IGNORE keyed on (event_id, espn_event_id). Returns True iff a new row was written
    (i.e. this key event hadn't been seen before for this match)."""
    cols = ", ".join(EVENT_FIELDS)
    qs = ", ".join("?" for _ in EVENT_FIELDS)
    cur = db.execute(f"INSERT OR IGNORE INTO key_events ({cols}) VALUES ({qs})",
                      tuple(row.get(f) for f in EVENT_FIELDS))
    return cur.rowcount > 0


def ensure_closing_table(db: sqlite3.Connection) -> None:
    cols = ", ".join(f"{f} {_coltype(f)}" for f in FIELDS)
    db.execute(f"CREATE TABLE IF NOT EXISTS closing_odds (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols}, "
               f"UNIQUE(event_id))")
    db.commit()


def upsert_closing_row(db: sqlite3.Connection, row: dict) -> None:
    """One closing snapshot per event: replaces any prior closing row for the same event_id (should
    never happen in practice — a match only transitions pre->in once — but makes this idempotent)."""
    cols = ", ".join(FIELDS)
    qs = ", ".join("?" for _ in FIELDS)
    db.execute(f"INSERT OR REPLACE INTO closing_odds ({cols}) VALUES ({qs})", tuple(row.get(f) for f in FIELDS))


# --------------------------------------------------------------------------- #
# polling loop
# --------------------------------------------------------------------------- #

class LiveOddsLogger:
    def __init__(self, db_path: str | None = None, interval: int = 60, slugs=DEFAULT_SLUGS,
                 window_hours: float = 3.0, slug_sweep_secs: float = 300.0, stats_interval: float = 120.0,
                 summary_secs: float | None = None):
        self.db_path = db_path or os.path.join(os.path.expanduser(os.getenv("HYDRA_HOME", "~/.hydra_scout")), "live_odds.sqlite")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        # Safety net: keep a dated copy of an existing DB before opening it (schema changes are additive only).
        if os.path.exists(self.db_path) and os.path.getsize(self.db_path) > 0:
            import shutil
            try:
                shutil.copy2(self.db_path, f"{self.db_path}.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.bak")
            except OSError:
                pass
        self.db = sqlite3.connect(self.db_path)
        ensure_table(self.db)
        ensure_stats_table(self.db)
        ensure_key_events_table(self.db)
        ensure_closing_table(self.db)
        self.interval = interval
        self.slugs = list(slugs)
        self.window_hours = window_hours
        self.slug_sweep_secs = slug_sweep_secs
        # summary_secs is a deprecated alias kept for scripts/callers built against the old kwarg;
        # one throttle now governs the shared /summary call (odds fallback + live_stats + key_events).
        self.stats_interval = summary_secs if summary_secs is not None else stats_interval
        self.session = httpx.Client(follow_redirects=True)
        self._last_slug_sweep = 0.0
        self._last_summary: dict[str, float] = {}
        self._prev_state: dict[str, str] = {}
        self._last_pre_row: dict[str, dict] = {}
        self._stat_names_logged = False
        self._stop = False

    def poll_once(self) -> int:
        merged: dict[str, tuple[dict, str | None, str | None]] = {}
        data = _get_json(self.session, f"{BASE}/all/scoreboard")
        if data:
            for e in data.get("events", []):
                merged[e.get("id")] = (e, None, None)
        if self.slugs and (time.time() - self._last_slug_sweep) >= self.slug_sweep_secs:
            self._last_slug_sweep = time.time()
            for slug in self.slugs:
                d = _get_json(self.session, f"{BASE}/{slug}/scoreboard", retries=1)
                if not d:
                    continue
                lg = (d.get("leagues") or [{}])[0]
                for e in d.get("events", []):
                    merged[e.get("id")] = (e, lg.get("slug") or slug, lg.get("name"))
        n_written = 0
        ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for event_id, (event, slug, name) in merged.items():
            if self._stop:
                break  # SIGTERM/SIGINT mid-poll: stop making new HTTP calls, commit what we have
            if not within_window(event, self.window_hours):
                # event fell out of the tracked window (finished, or too far out): stop tracking it
                if self._prev_state.pop(event_id, None) is not None:
                    self._last_pre_row.pop(event_id, None)
                    self._last_summary.pop(event_id, None)
                continue
            row = extract_row(event, slug, name)
            if row is None or not event_id:
                continue
            summary = None
            if row["state"] == "in" and self._summary_due(event_id):
                summary = self._fetch_summary(event_id, row.get("league_slug") or slug)
            if row["state"] == "in" and not row["provider"] and summary:
                self._apply_summary_odds(row, summary)
            row["ts_utc"] = ts_utc
            self._track_closing(event_id, row)
            if not row_unchanged(self.db, event_id, row):
                insert_row(self.db, row)
                n_written += 1
            if summary:
                self._store_stats_and_events(summary, event_id, row)
        self.db.commit()
        return n_written

    def _summary_due(self, event_id: str) -> bool:
        return (time.time() - self._last_summary.get(event_id, 0.0)) >= self.stats_interval

    def _fetch_summary(self, event_id: str, slug: str | None) -> dict | None:
        self._last_summary[event_id] = time.time()
        return _get_json(self.session, f"{BASE}/{slug or 'all'}/summary?event={event_id}", retries=1)

    def _apply_summary_odds(self, row: dict, summary: dict) -> None:
        pc = (summary or {}).get("pickcenter") or []
        if not pc:
            return
        o0 = pc[0]
        mh, md, ma = moneyline(o0)
        ou_line, over_odds, under_odds = total_ou(o0)
        spread, spread_odds = point_spread(o0)
        row.update(
            provider=(o0.get("provider") or {}).get("name") or row["provider"],
            ml_home=mh if mh is not None else row["ml_home"], ml_draw=md if md is not None else row["ml_draw"],
            ml_away=ma if ma is not None else row["ml_away"],
            ml_home_dec=american_to_decimal(mh) or row["ml_home_dec"], ml_draw_dec=american_to_decimal(md) or row["ml_draw_dec"],
            ml_away_dec=american_to_decimal(ma) or row["ml_away_dec"],
            ou_line=ou_line if ou_line is not None else row["ou_line"],
            over_odds=over_odds if over_odds is not None else row["over_odds"],
            under_odds=under_odds if under_odds is not None else row["under_odds"],
            spread=spread if spread is not None else row["spread"], spread_odds=spread_odds if spread_odds is not None else row["spread_odds"])

    def _track_closing(self, event_id: str, row: dict) -> None:
        """Captures the last pre-match live_odds row (or, failing that, the first in-play one) into
        closing_odds the moment an event's state first flips pre -> in. An event already `in` the
        first time we ever see it (logger started mid-match) has no pre-match row to capture, so it
        is silently skipped — there is no closing line to record."""
        prev_state = self._prev_state.get(event_id)
        if row["state"] == "pre":
            self._last_pre_row[event_id] = dict(row)
        elif row["state"] == "in" and prev_state == "pre":
            closing = self._last_pre_row.pop(event_id, None) or row
            upsert_closing_row(self.db, closing)
        self._prev_state[event_id] = row["state"]

    def _store_stats_and_events(self, summary: dict, event_id: str, row: dict) -> None:
        for srow in parse_boxscore_stats(summary, event_id, row.get("league_slug"), row["state"],
                                          row["period"], row["clock"], row["score_home"], row["score_away"]):
            srow["ts_utc"] = row["ts_utc"]
            if stats_row_unchanged(self.db, event_id, srow.get("team_id"), srow):
                continue
            insert_stats_row(self.db, srow)
            if not self._stat_names_logged and srow.get("raw_stats_json"):
                names = sorted(json.loads(srow["raw_stats_json"]).keys())
                print(f"[live_odds_logger] boxscore stat names available: {names}", flush=True)
                self._stat_names_logged = True
        n_new = 0
        for erow in parse_key_events(summary, event_id, row.get("league_slug")):
            erow["ts_utc"] = row["ts_utc"]
            if insert_key_event(self.db, erow):
                n_new += 1
        if n_new:
            print(f"[live_odds_logger] {event_id}: {n_new} new key event(s)", flush=True)

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            print(f"[live_odds_logger] received signal {signum}, finishing current tick and exiting", flush=True)
            self._stop = True
        try:
            signal.signal(signal.SIGTERM, _handler)
            signal.signal(signal.SIGINT, _handler)
        except (ValueError, OSError):
            pass  # not the main thread, or signals unsupported on this platform — best-effort only

    def run(self, minutes: float = 90.0, until: datetime | None = None) -> None:
        end = until.timestamp() if until is not None else time.time() + minutes * 60
        self._install_signal_handlers()
        last_heartbeat = time.time()
        n_polls = n_total = 0
        while time.time() < end and not self._stop:
            t0 = time.time()
            try:
                n = self.poll_once()
                n_total += n
                n_polls += 1
                print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] wrote {n} row(s) -> {self.db_path}", flush=True)
            except Exception as e:
                print(f"[live_odds_logger] tick failed, continuing: {e!r}", file=sys.stderr, flush=True)
            if time.time() - last_heartbeat >= 600:
                last_heartbeat = time.time()
                n_live = sum(1 for s in self._prev_state.values() if s == "in")
                print(f"[live_odds_logger] heartbeat: {n_polls} poll(s), {n_total} row(s) written, "
                      f"{len(self._prev_state)} event(s) tracked, {n_live} live", flush=True)
            if self._stop:
                break
            sleep_for = max(0.0, self.interval - (time.time() - t0))
            sleep_for = max(0.0, min(sleep_for, end - time.time()))
            time.sleep(sleep_for)
        print(f"[live_odds_logger] exiting cleanly after {n_polls} poll(s), {n_total} row(s) written", flush=True)


def _parse_until(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"--until must look like 'YYYY-MM-DDTHH:MM:SSZ' (UTC), got {s!r}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Poll ESPN soccer scoreboard odds/stats/events into sqlite.")
    p.add_argument("--interval", type=int, default=60, help="seconds between polls of /all/scoreboard (default 60)")
    p.add_argument("--minutes", type=float, default=90.0, help="total run time in minutes (default 90; ignored if --until is given)")
    p.add_argument("--until", type=_parse_until, default=None,
                   help="stop at this UTC timestamp instead of after --minutes, e.g. 2026-09-27T06:00:00Z")
    p.add_argument("--slugs", default=",".join(DEFAULT_SLUGS), help="comma-separated extra league slugs, '' to disable")
    p.add_argument("--window-hours", type=float, default=3.0, help="how far ahead a 'pre' event is logged (default 3h)")
    p.add_argument("--stats-interval", type=float, default=120.0,
                   help="seconds between /summary pulls (live_stats + key_events + odds fallback) per live event (default 120)")
    p.add_argument("--summary-secs", type=float, default=None, help=argparse.SUPPRESS)  # deprecated alias
    p.add_argument("--db", default=None, help="sqlite path (default ~/.hydra_scout/live_odds.sqlite)")
    args = p.parse_args(argv)
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
    logger = LiveOddsLogger(db_path=args.db, interval=args.interval, slugs=slugs, window_hours=args.window_hours,
                             stats_interval=args.stats_interval, summary_secs=args.summary_secs)
    schedule = f"until={args.until:%Y-%m-%dT%H:%M:%SZ}" if args.until else f"minutes={args.minutes}"
    print(f"[live_odds_logger] db={logger.db_path} interval={args.interval}s {schedule} "
          f"window={args.window_hours}h stats_interval={logger.stats_interval}s slugs={slugs or 'none'}", flush=True)
    logger.run(minutes=args.minutes, until=args.until)
    sys.exit(0)


if __name__ == "__main__":
    main()
