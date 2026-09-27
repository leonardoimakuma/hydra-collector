import csv
import gzip
import os
import sqlite3

from hydra_scout.collect import export
from hydra_scout.collect import live_odds_logger as lol


def _odds_row(event_id, ts_utc, ml_home=170):
    row = {f: None for f in lol.FIELDS}
    row.update(event_id=event_id, ts_utc=ts_utc, state="in", ml_home=ml_home)
    return row


def _make_db(path):
    db = sqlite3.connect(path)
    lol.ensure_table(db)
    lol.ensure_stats_table(db)
    lol.ensure_key_events_table(db)
    lol.ensure_closing_table(db)
    lol.ensure_lineups_table(db)
    return db


def _read_gz_rows(path):
    with gzip.open(path, "rt", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- query_day_rows / day filtering

def test_query_day_rows_filters_by_utc_day(tmp_path):
    db_path = str(tmp_path / "live_odds.sqlite")
    db = _make_db(db_path)
    lol.insert_row(db, _odds_row("E1", "2026-09-27T00:05:00Z"))
    lol.insert_row(db, _odds_row("E1", "2026-09-27T23:55:00Z"))
    lol.insert_row(db, _odds_row("E2", "2026-09-28T00:05:00Z"))
    db.commit()

    rows_27 = export.query_day_rows(db, "live_odds", lol.FIELDS, "ts_utc", "2026-09-27")
    rows_28 = export.query_day_rows(db, "live_odds", lol.FIELDS, "ts_utc", "2026-09-28")
    assert len(rows_27) == 2 and len(rows_28) == 1
    assert {r["event_id"] for r in rows_27} == {"E1"}


def test_query_day_rows_missing_table_returns_empty(tmp_path):
    db = sqlite3.connect(str(tmp_path / "empty.sqlite"))
    assert export.query_day_rows(db, "live_stats", lol.STATS_FIELDS, "ts_utc", "2026-09-27") == []


# ---------------------------------------------------------------- merge_dedupe

def test_merge_dedupe_keeps_existing_and_adds_new_keys_only():
    existing = [{"event_id": "E1", "ts_utc": "T1", "ml_home": 170}]
    new = [
        {"event_id": "E1", "ts_utc": "T1", "ml_home": 999},   # same key as existing -> existing wins
        {"event_id": "E1", "ts_utc": "T2", "ml_home": 180},   # new key -> added
    ]
    merged = export.merge_dedupe(existing, new, ("event_id", "ts_utc"))
    assert len(merged) == 2
    assert merged[0] == {"event_id": "E1", "ts_utc": "T1", "ml_home": 170}  # untouched, not overwritten
    assert merged[1]["ts_utc"] == "T2"


def test_merge_dedupe_empty_existing():
    new = [{"event_id": "E1", "ts_utc": "T1"}]
    assert export.merge_dedupe([], new, ("event_id", "ts_utc")) == new


# ---------------------------------------------------------------- export_day: full round trip + re-run safety

def test_export_day_writes_all_five_files(tmp_path):
    db_path = str(tmp_path / "live_odds.sqlite")
    out_dir = str(tmp_path / "out")
    db = _make_db(db_path)
    lol.insert_row(db, _odds_row("E1", "2026-09-27T00:05:00Z"))
    lol.insert_stats_row(db, {**{f: None for f in lol.STATS_FIELDS},
                               "ts_utc": "2026-09-27T00:05:00Z", "event_id": "E1", "team_id": "1", "shots": 3})
    lol.insert_key_event(db, {**{f: None for f in lol.EVENT_FIELDS},
                               "ts_utc": "2026-09-27T00:05:00Z", "event_id": "E1", "espn_event_id": "K1", "type": "Goal"})
    lol.upsert_closing_row(db, _odds_row("E1", "2026-09-27T00:00:00Z"))
    lol.insert_lineup_rows(db, [{**{f: None for f in lol.LINEUP_FIELDS}, "ts_utc": "2026-09-27T00:01:00Z",
                                 "event_id": "E1", "team_id": "1", "athlete_id": "9", "player": "P", "starter": 1}])
    db.commit()
    db.close()

    counts = export.export_day("2026-09-27", db_path=db_path, out_dir=out_dir)
    assert counts == {"odds": 1, "stats": 1, "events": 1, "closing": 1, "lineups": 1}
    for name in ("odds", "stats", "events", "closing", "lineups"):
        path = os.path.join(out_dir, "2026-09-27", f"{name}.csv.gz")
        assert os.path.exists(path)
        rows = _read_gz_rows(path)
        assert len(rows) == 1
        assert rows[0]["event_id"] == "E1"


def test_export_day_no_db_yet_returns_zero_counts(tmp_path):
    counts = export.export_day("2026-09-27", db_path=str(tmp_path / "nope.sqlite"), out_dir=str(tmp_path / "out"))
    assert counts == {"odds": 0, "stats": 0, "events": 0, "closing": 0, "lineups": 0}
    assert not os.path.exists(os.path.join(str(tmp_path / "out"), "2026-09-27"))


def test_export_day_rerun_is_append_safe_no_duplicates(tmp_path):
    """Simulates two GitHub Actions jobs the same UTC day: each has its own fresh sqlite (only this
    run's rows), and the second run's checkout already has the first run's committed CSV."""
    out_dir = str(tmp_path / "out")

    db1_path = str(tmp_path / "run1.sqlite")
    db1 = _make_db(db1_path)
    lol.insert_row(db1, _odds_row("E1", "2026-09-27T00:05:00Z", ml_home=170))
    lol.insert_row(db1, _odds_row("E1", "2026-09-27T00:06:00Z", ml_home=175))
    db1.commit(); db1.close()
    counts1 = export.export_day("2026-09-27", db_path=db1_path, out_dir=out_dir)
    assert counts1["odds"] == 2

    # run 2: a fresh sqlite with one overlapping tick (same event+ts, re-collected) and one new tick
    db2_path = str(tmp_path / "run2.sqlite")
    db2 = _make_db(db2_path)
    lol.insert_row(db2, _odds_row("E1", "2026-09-27T00:06:00Z", ml_home=175))  # duplicate of run 1
    lol.insert_row(db2, _odds_row("E1", "2026-09-27T06:10:00Z", ml_home=300))  # genuinely new
    db2.commit(); db2.close()
    counts2 = export.export_day("2026-09-27", db_path=db2_path, out_dir=out_dir)
    assert counts2["odds"] == 3  # 2 from run 1 + 1 new, the overlap not double-counted

    rows = _read_gz_rows(os.path.join(out_dir, "2026-09-27", "odds.csv.gz"))
    assert len(rows) == 3
    ts_values = sorted(r["ts_utc"] for r in rows)
    assert ts_values == ["2026-09-27T00:05:00Z", "2026-09-27T00:06:00Z", "2026-09-27T06:10:00Z"]


def test_export_day_closing_dedupes_on_event_id_only(tmp_path):
    out_dir = str(tmp_path / "out")
    db_path = str(tmp_path / "live_odds.sqlite")
    db = _make_db(db_path)
    lol.upsert_closing_row(db, _odds_row("E1", "2026-09-27T00:00:00Z", ml_home=170))
    db.commit(); db.close()
    export.export_day("2026-09-27", db_path=db_path, out_dir=out_dir)

    # a second run's sqlite happens to have re-derived the very same event's closing row
    db2_path = str(tmp_path / "live_odds2.sqlite")
    db2 = _make_db(db2_path)
    lol.upsert_closing_row(db2, _odds_row("E1", "2026-09-27T00:00:00Z", ml_home=170))
    db2.commit(); db2.close()
    counts = export.export_day("2026-09-27", db_path=db2_path, out_dir=out_dir)
    assert counts["closing"] == 1  # not duplicated


def test_export_lineups_old_db_without_table_is_safe(tmp_path):
    """A sqlite file written before the lineups table existed still exports (lineups -> 0)."""
    db_path = str(tmp_path / "old.sqlite")
    db = sqlite3.connect(db_path)
    lol.ensure_table(db)
    lol.insert_row(db, _odds_row("E1", "2026-09-27T00:05:00Z"))
    db.commit(); db.close()
    counts = export.export_day("2026-09-27", db_path=db_path, out_dir=str(tmp_path / "out"))
    assert counts["odds"] == 1 and counts["lineups"] == 0
