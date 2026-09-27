import json
import os
import sqlite3
from datetime import datetime, timezone

from hydra_scout.collect import live_odds_logger as lol

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "scoreboard_sample.json")
SUMMARY_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "summary_sample.json")
ROSTERS_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "summary_rosters_sample.json")
PRELINEUP_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "summary_prelineup_sample.json")
PREKICKOFF_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "summary_prekickoff_lineups_sample.json")


def _events():
    return json.load(open(FIXTURE))["events"]


def _by_id(events, event_id):
    return next(e for e in events if e["id"] == event_id)


def _summary():
    return json.load(open(SUMMARY_FIXTURE))


# ---------------------------------------------------------------- extraction (real captured payloads)

def test_extract_row_in_play_prefers_current_snapshot():
    """Real ESPN payload, captured 2026-09-26 mid-match: moneyline/total/spread all carry a
    'current' snapshot distinct from 'close' once a match is live, and extract_row must prefer it."""
    e = _by_id(_events(), "401883166")
    odds0 = e["competitions"][0]["odds"][0]
    assert odds0["moneyline"]["draw"]["current"]["odds"] != odds0["moneyline"]["draw"]["close"]["odds"]  # sanity: they DID move

    row = lol.extract_row(e)
    assert row["state"] == "in" and row["provider"] == "DraftKings"
    assert row["home"] == "Ceuta" and row["away"] == "Real Sociedad II"
    assert row["ml_home"] == int(odds0["moneyline"]["home"]["current"]["odds"])
    assert row["ml_draw"] == int(odds0["moneyline"]["draw"]["current"]["odds"])
    assert row["ml_away"] == int(odds0["moneyline"]["away"]["current"]["odds"])
    assert row["ou_line"] == 1.5   # current line moved off the pre-match 2.5
    assert row["spread"] == 0.5
    assert row["league_slug"] == "esp.2"
    assert row["score_home"] == 0 and row["score_away"] == 0
    assert row["clock"] == "29'" and row["period"] == 1


def test_extract_row_pre_match_uses_close_snapshot():
    e = _by_id(_events(), "401861057")
    row = lol.extract_row(e)
    assert row["state"] == "pre"
    odds0 = e["competitions"][0]["odds"][0]
    assert "current" not in odds0["moneyline"]["home"]   # not kicked off yet: no live snapshot
    assert row["ml_home"] == int(odds0["moneyline"]["home"]["close"]["odds"])
    assert row["league_slug"] == "uefa.nations"
    assert row["league_name"] == "UEFA Nations League, Group B1"


def test_extract_row_survives_null_odds_entry():
    """ESPN sometimes sends "odds": [None]; must not crash, just come back empty."""
    e = _by_id(_events(), "401922335")
    assert e["competitions"][0]["odds"] == [None]
    row = lol.extract_row(e)
    assert row["provider"] is None and row["ml_home"] is None and row["ou_line"] is None


def test_decimal_conversion():
    assert lol.american_to_decimal(170) == 2.70
    assert lol.american_to_decimal(-225) == round(1 + 100 / 225, 4)
    assert lol.american_to_decimal(None) is None


def test_league_override_takes_priority_over_inferred_slug():
    e = _by_id(_events(), "401883166")
    row = lol.extract_row(e, league_slug="esp.2", league_name="Spanish Segunda")
    assert row["league_slug"] == "esp.2" and row["league_name"] == "Spanish Segunda"


# ---------------------------------------------------------------- time window (synthetic, clock-independent)

def test_within_window_live_always_true():
    e = {"competitions": [{"status": {"type": {"state": "in"}}}]}
    assert lol.within_window(e) is True


def test_within_window_pre_match_kickoff_horizon():
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    soon = {"competitions": [{"status": {"type": {"state": "pre"}}}], "date": "2026-09-26T14:30Z"}   # 2.5h out
    far = {"competitions": [{"status": {"type": {"state": "pre"}}}], "date": "2026-09-26T17:00Z"}    # 5h out
    just_kicked = {"competitions": [{"status": {"type": {"state": "pre"}}}], "date": "2026-09-26T11:50Z"}  # 10m ago, state lagging
    assert lol.within_window(soon, hours=3.0, now=now) is True
    assert lol.within_window(far, hours=3.0, now=now) is False
    assert lol.within_window(just_kicked, hours=3.0, now=now) is True


def test_within_window_finished_is_false():
    e = {"competitions": [{"status": {"type": {"state": "post"}}}], "date": "2026-09-26T09:00Z"}
    assert lol.within_window(e) is False


# ---------------------------------------------------------------- storage / dedup

def test_row_unchanged_and_insert_roundtrip(tmp_path):
    db = sqlite3.connect(str(tmp_path / "live_odds.sqlite"))
    lol.ensure_table(db)
    row = {f: None for f in lol.FIELDS}
    row.update(ts_utc="2026-09-26T12:00:00Z", event_id="E1", state="in", ml_home=170)
    assert lol.row_unchanged(db, "E1", row) is False
    lol.insert_row(db, row)
    db.commit()

    # same content, later ts -> unchanged (must be skipped by callers)
    row2 = dict(row, ts_utc="2026-09-26T12:01:00Z")
    assert lol.row_unchanged(db, "E1", row2) is True

    # odds actually moved -> changed
    row3 = dict(row, ts_utc="2026-09-26T12:02:00Z", ml_home=180)
    assert lol.row_unchanged(db, "E1", row3) is False

    # a different event is never "unchanged" against E1's history
    assert lol.row_unchanged(db, "E2", row) is False


# ---------------------------------------------------------------- boxscore stat parsing (real captured /summary)

def test_parse_boxscore_stats_extracts_named_fields_and_raw_json():
    """Real ESPN /summary payload, captured 2026-09-26 mid-match (Cruz Azul 3-2 Toluca, min 86')."""
    rows = lol.parse_boxscore_stats(_summary(), event_id="401876959", league_slug="mex.1", state="in",
                                     period=2, clock="86'", score_home=3, score_away=2)
    assert len(rows) == 2
    home = next(r for r in rows if r["team_side"] == "home")
    away = next(r for r in rows if r["team_side"] == "away")

    assert home["team_id"] == "218" and home["team_name"] == "Cruz Azul"
    assert home["shots"] == 12 and home["shots_on_target"] == 5 and home["possession_pct"] == 48.5
    assert home["corners"] == 1 and home["fouls"] == 12 and home["yellow_cards"] == 3 and home["red_cards"] == 0
    assert home["saves"] == 4 and home["offsides"] == 3
    assert home["event_id"] == "401876959" and home["league_slug"] == "mex.1"
    assert home["state"] == "in" and home["period"] == 2 and home["clock"] == "86'"
    assert home["score_home"] == 3 and home["score_away"] == 2

    assert away["team_id"] == "223" and away["team_name"] == "Toluca"
    assert away["shots"] == 17 and away["possession_pct"] == 51.5

    # the full raw ESPN stat set survives verbatim, not just the named subset
    raw = json.loads(home["raw_stats_json"])
    assert raw["totalShots"] == "12" and "accuratePasses" in raw and "totalTackles" in raw


def test_parse_boxscore_stats_empty_boxscore_is_safe():
    assert lol.parse_boxscore_stats({}, "E1", None, "in", 1, "10'", 0, 0) == []
    assert lol.parse_boxscore_stats(None, "E1", None, "in", 1, "10'", 0, 0) == []
    assert lol.parse_boxscore_stats({"boxscore": {"teams": [None]}}, "E1", None, "in", 1, "10'", 0, 0) == []


def test_parse_boxscore_stats_tolerates_missing_individual_stat():
    summary = {"boxscore": {"teams": [
        {"team": {"id": "1", "displayName": "A"}, "homeAway": "home",
         "statistics": [{"name": "totalShots", "displayValue": "4"}]},  # no possessionPct etc.
    ]}}
    row = lol.parse_boxscore_stats(summary, "E1", "eng.1", "in", 1, "20'", 0, 0)[0]
    assert row["shots"] == 4
    assert row["possession_pct"] is None and row["yellow_cards"] is None


# ---------------------------------------------------------------- key events (real captured /summary)

def test_parse_key_events_filters_to_goals_cards_penalties_subs():
    rows = lol.parse_key_events(_summary(), event_id="401876959", league_slug="mex.1")
    types = [r["type_slug"] for r in rows]
    # tracked: 3 plain goals + 1 headed goal, 1 penalty-scored, 5 substitutions; cards excluded
    assert types.count("goal") == 3
    assert "goal---header" in types
    assert "penalty---scored" in types
    assert types.count("substitution") == 5
    # excluded chatter never leaks through
    assert not any(t in ("kickoff", "halftime", "start-2nd-half", "start-delay", "end-delay", "yellow-card") for t in types)

    goal = next(r for r in rows if r["type_slug"] == "goal")
    assert goal["team_name"] in ("Cruz Azul", "Toluca")
    assert goal["minute"] and goal["period"] == 1
    assert goal["scoring_play"] == 1
    assert goal["espn_event_id"] and goal["event_id"] == "401876959" and goal["league_slug"] == "mex.1"


def test_is_tracked_key_event_covers_red_and_own_goal_variants():
    assert lol._is_tracked_key_event("red-card") is True
    assert lol._is_tracked_key_event("own-goal") is True
    assert lol._is_tracked_key_event("goal---volley") is True
    assert lol._is_tracked_key_event("substitution") is True
    assert lol._is_tracked_key_event("yellow-card") is False
    assert lol._is_tracked_key_event("start-delay") is False
    assert lol._is_tracked_key_event(None) is False


def test_parse_key_events_player_from_first_participant():
    summary = {"keyEvents": [{
        "id": "1", "type": {"text": "Goal", "type": "goal"}, "period": {"number": 1},
        "clock": {"displayValue": "10'"}, "scoringPlay": True, "team": {"id": "1", "displayName": "A"},
        "text": "Goal! A 1, B 0.", "wallclock": "2026-09-26T22:00:00Z",
        "participants": [{"athlete": {"id": "9", "displayName": "Scorer Name"}}, {"athlete": {"id": "8", "displayName": "Assist Name"}}],
    }]}
    row = lol.parse_key_events(summary, "E1", "eng.1")[0]
    assert row["player"] == "Scorer Name"
    assert row["text"].startswith("Goal!")


def test_parse_key_events_empty_is_safe():
    assert lol.parse_key_events({}, "E1", None) == []
    assert lol.parse_key_events(None, "E1", None) == []
    assert lol.parse_key_events({"keyEvents": [None]}, "E1", None) == []


# ---------------------------------------------------------------- live_stats / key_events storage

def test_live_stats_ensure_dedup_insert_roundtrip(tmp_path):
    db = sqlite3.connect(str(tmp_path / "t.sqlite"))
    lol.ensure_stats_table(db)
    row = {f: None for f in lol.STATS_FIELDS}
    row.update(ts_utc="2026-09-26T22:00:00Z", event_id="E1", team_id="218", team_side="home", shots=5)
    assert lol.stats_row_unchanged(db, "E1", "218", row) is False
    lol.insert_stats_row(db, row)
    db.commit()

    row2 = dict(row, ts_utc="2026-09-26T22:01:00Z")  # only ts differs -> unchanged
    assert lol.stats_row_unchanged(db, "E1", "218", row2) is True

    row3 = dict(row, ts_utc="2026-09-26T22:02:00Z", shots=6)  # shots moved -> changed
    assert lol.stats_row_unchanged(db, "E1", "218", row3) is False

    # the away team's first row is never "unchanged" against the home team's history
    assert lol.stats_row_unchanged(db, "E1", "223", row) is False


def test_key_events_insert_or_ignore_dedupes_on_espn_id(tmp_path):
    db = sqlite3.connect(str(tmp_path / "t.sqlite"))
    lol.ensure_key_events_table(db)
    row = {f: None for f in lol.EVENT_FIELDS}
    row.update(ts_utc="2026-09-26T22:00:00Z", event_id="E1", espn_event_id="52906527", type="Goal")
    assert lol.insert_key_event(db, row) is True
    db.commit()
    # re-parsing the same summary later in the poll loop must not duplicate it
    assert lol.insert_key_event(db, dict(row, ts_utc="2026-09-26T22:01:00Z")) is False
    n = db.execute("SELECT COUNT(*) FROM key_events").fetchone()[0]
    assert n == 1
    # same espn id but a different match is a distinct row
    assert lol.insert_key_event(db, dict(row, event_id="E2", ts_utc="2026-09-26T22:01:00Z")) is True


# ---------------------------------------------------------------- closing-snapshot logic

def _mk_logger(tmp_path):
    return lol.LiveOddsLogger(db_path=str(tmp_path / "closing.sqlite"), slugs=[])


def _row(event_id, state, **overrides):
    row = {f: None for f in lol.FIELDS}
    row.update(event_id=event_id, state=state, ts_utc="2026-09-26T12:00:00Z", ml_home=170)
    row.update(overrides)
    return row


def test_closing_captured_on_pre_to_in_transition(tmp_path):
    logger = _mk_logger(tmp_path)
    pre = _row("E1", "pre", ts_utc="2026-09-26T12:55:00Z", ml_home=170)
    logger._track_closing("E1", pre)
    assert logger.db.execute("SELECT COUNT(*) FROM closing_odds").fetchone()[0] == 0  # not yet: still pre

    live = _row("E1", "in", ts_utc="2026-09-26T13:05:00Z", ml_home=210)  # odds moved once live
    logger._track_closing("E1", live)
    rows = logger.db.execute("SELECT event_id, ts_utc, ml_home FROM closing_odds").fetchall()
    assert rows == [("E1", "2026-09-26T12:55:00Z", 170)]  # captured the LAST PRE row, not the live one

    # a later in-play tick must not re-trigger or overwrite the closing row
    later = _row("E1", "in", ts_utc="2026-09-26T13:10:00Z", ml_home=250)
    logger._track_closing("E1", later)
    rows = logger.db.execute("SELECT event_id, ts_utc, ml_home FROM closing_odds").fetchall()
    assert rows == [("E1", "2026-09-26T12:55:00Z", 170)]


def test_closing_skipped_when_first_seen_already_in_play(tmp_path):
    """Logger started mid-match: no pre-match row was ever captured, so there's nothing to record."""
    logger = _mk_logger(tmp_path)
    live = _row("E2", "in", ml_home=140)
    logger._track_closing("E2", live)
    assert logger.db.execute("SELECT COUNT(*) FROM closing_odds").fetchone()[0] == 0


def test_closing_falls_back_to_current_row_if_no_pre_row_cached(tmp_path):
    """Defensive fallback: if somehow no pre-match row was cached (e.g. a very fast pre->in flip
    between polls) but we did observe the 'pre' state, the current row is used rather than dropping
    the closing snapshot entirely."""
    logger = _mk_logger(tmp_path)
    logger._prev_state["E3"] = "pre"  # simulate having seen 'pre' without a cached row
    live = _row("E3", "in", ml_home=180)
    logger._track_closing("E3", live)
    rows = logger.db.execute("SELECT event_id, ml_home FROM closing_odds").fetchall()
    assert rows == [("E3", 180)]


# ---------------------------------------------------------------- lineups (real captured /summary rosters)

def _rosters():
    return json.load(open(ROSTERS_FIXTURE))


def test_parse_lineups_real_rosters():
    """Real ESPN /summary (WSL Manchester United v West Ham, 2026-09-27): 20 listed per side, 11 starters."""
    rows = lol.parse_lineups(_rosters(), event_id="401902905", league_slug="eng.w.1", state="pre")
    assert len(rows) == 40
    home = [r for r in rows if r["team_side"] == "home"]
    assert {r["team_name"] for r in home} == {"Manchester United"} and home[0]["formation"] == "4-2-3-1"
    assert sum(r["starter"] for r in home) == 11 and sum(r["starter"] for r in rows) == 22
    gk = home[0]
    assert gk["player"] == "Phallon Tullis-Joyce" and gk["athlete_id"] == "208938"
    assert gk["position"] == "Goalkeeper" and gk["position_abbr"] == "G" and gk["jersey"] == "91"
    assert gk["starter"] == 1 and gk["formation_place"] == "1" and gk["state"] == "pre"
    assert set(rows[0]) == set(lol.LINEUP_FIELDS) - {"ts_utc"}
    assert lol.lineups_confirmed(rows) is True


def test_parse_lineups_real_pre_kickoff_payload():
    """Real ESPN /summary 58 min before kickoff (WSL Chelsea v Arsenal, 2026-09-27 14:32Z): teams are out,
    and subbedIn/subbedOut come as {"didSub": false} dicts rather than the post-match bools."""
    pre = json.load(open(PREKICKOFF_FIXTURE))
    assert pre["header"]["competitions"][0]["status"]["type"]["state"] == "pre"
    rows = lol.parse_lineups(pre, "401902901", "eng.w.1", "pre")
    assert len(rows) == 40 and sum(r["starter"] for r in rows) == 22
    assert {r["formation"] for r in rows if r["team_side"] == "home"} == {"4-3-3"}
    assert all(r["subbed_in"] == 0 and r["subbed_out"] == 0 for r in rows)
    assert lol.lineups_confirmed(rows)
    assert lol._flag({"didSub": True}) == 1 and lol._flag(True) == 1 and lol._flag(None) == 0


def test_parse_lineups_before_announcement_is_empty():
    pre = json.load(open(PRELINEUP_FIXTURE))
    assert pre["rosters"] and all("roster" not in t or not t["roster"] for t in pre["rosters"])
    assert lol.parse_lineups(pre, "401902901", "eng.w.1", "pre") == []
    assert lol.parse_lineups(None, "E", None, "pre") == [] and lol.parse_lineups({"rosters": [None]}, "E", None, "pre") == []
    assert lol.lineups_confirmed([]) is False
    partial = [{"team_id": "1", "starter": 1}] * 10
    assert lol.lineups_confirmed(partial) is False


def test_lineups_insert_or_ignore_keeps_first_seen(tmp_path):
    db = sqlite3.connect(str(tmp_path / "t.sqlite"))
    lol.ensure_lineups_table(db)
    rows = lol.parse_lineups(_rosters(), "401902905", "eng.w.1", "pre")
    for r in rows:
        r["ts_utc"] = "2026-09-27T11:00:00Z"
    assert lol.insert_lineup_rows(db, rows) == 40
    later = [dict(r, ts_utc="2026-09-27T11:05:00Z", state="in") for r in rows]
    assert lol.insert_lineup_rows(db, later) == 0
    assert db.execute("SELECT DISTINCT ts_utc, state FROM lineups").fetchall() == [("2026-09-27T11:00:00Z", "pre")]
    assert lol.has_lineups(db, "401902905") and not lol.has_lineups(db, "other")


def _pre_event(event_id, kickoff):
    return {"id": event_id, "date": kickoff, "competitions": [{"status": {"type": {"state": "pre"}}}]}


def test_lineup_due_window_interval_and_done(tmp_path):
    logger = lol.LiveOddsLogger(db_path=str(tmp_path / "l.sqlite"), slugs=[], lineup_window_min=75, lineup_interval=300)
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    assert logger._lineup_due("E1", _pre_event("E1", "2026-09-27T13:00Z"), now=now) is True      # 60 min out
    assert logger._lineup_due("E2", _pre_event("E2", "2026-09-27T14:00Z"), now=now) is False     # 120 min out
    logger._last_lineup_poll["E1"] = __import__("time").time()
    assert logger._lineup_due("E1", _pre_event("E1", "2026-09-27T13:00Z"), now=now) is False     # polled < 300 s ago
    logger._lineups_done.add("E3")
    assert logger._lineup_due("E3", _pre_event("E3", "2026-09-27T12:30Z"), now=now) is False     # already captured
    off = lol.LiveOddsLogger(db_path=str(tmp_path / "o.sqlite"), slugs=[], lineup_window_min=0)
    assert off._lineup_due("E1", _pre_event("E1", "2026-09-27T12:30Z"), now=now) is False


def test_store_lineups_once_per_event(tmp_path):
    logger = lol.LiveOddsLogger(db_path=str(tmp_path / "s.sqlite"), slugs=[])
    row = {"ts_utc": "2026-09-27T11:00:00Z", "league_slug": "eng.w.1", "state": "pre"}
    logger._store_lineups(json.load(open(PRELINEUP_FIXTURE)), "401902905", row)   # not announced yet
    assert "401902905" not in logger._lineups_done
    logger._store_lineups(_rosters(), "401902905", row)
    assert "401902905" in logger._lineups_done
    logger._store_lineups(_rosters(), "401902905", dict(row, ts_utc="2026-09-27T11:05:00Z", state="in"))
    assert logger.db.execute("SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM lineups").fetchone() == (40, "2026-09-27T11:00:00Z", "2026-09-27T11:00:00Z")
    # a fresh logger on the same db recognises the event as done without another insert
    logger.db.commit()
    again = lol.LiveOddsLogger(db_path=str(tmp_path / "s.sqlite"), slugs=[])
    assert again._lineups_known("401902905") is True


def test_poll_once_fetches_pre_match_lineups(tmp_path, monkeypatch):
    """poll_once: a 'pre' event 30 min out triggers one /summary call for lineups; the same tick does
    not re-poll it and a second tick inside lineup_interval doesn't either."""
    ko = (datetime.now(timezone.utc).replace(second=0, microsecond=0) + __import__("datetime").timedelta(minutes=30))
    event = {"id": "401902901", "date": ko.strftime("%Y-%m-%dT%H:%MZ"),
             "competitions": [{"status": {"type": {"state": "pre"}}, "competitors": [], "odds": []}]}
    calls = []

    def fake_get(session, url, timeout=15.0, retries=3):
        calls.append(url)
        if "scoreboard" in url:
            return {"events": [event]}
        return json.load(open(PREKICKOFF_FIXTURE))
    monkeypatch.setattr(lol, "_get_json", fake_get)
    logger = lol.LiveOddsLogger(db_path=str(tmp_path / "p.sqlite"), slugs=[])
    logger.poll_once()
    assert sum("summary" in u for u in calls) == 1
    assert logger.db.execute("SELECT COUNT(*), MIN(state) FROM lineups").fetchone() == (40, "pre")
    logger.poll_once()
    assert sum("summary" in u for u in calls) == 1        # captured: no further lineup polls
