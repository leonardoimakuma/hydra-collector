"""
Exports the live_odds_logger sqlite tables (live_odds, live_stats, key_events, closing_odds, lineups)
for one UTC calendar day into gzipped CSVs under <out-dir>/<day>/{odds,stats,events,closing,lineups}.csv.gz.

Built to be append-safe across repeated runs against the same day: a GitHub Actions job only ever
holds one run's worth of sqlite data (each job starts a fresh sqlite file), but up to four jobs a day
(00/06/12/18 UTC) can each contribute rows for the same UTC day, and each job's checkout already has
whatever an earlier job committed. So export_day reads back the day's existing CSV (if the checkout
has one), reads the fresh rows out of this run's sqlite, de-duplicates the union on that table's
natural key, and rewrites the file — never dropping rows a previous run already committed, never
duplicating a row both runs happen to see (e.g. a match that straddles the 00:00 UTC boundary).

CLI:
  python -m hydra_scout.collect.export --day 2026-09-27
  python -m hydra_scout.collect.export --day 2026-09-27 --db ~/.hydra_scout/live_odds.sqlite --out-dir data/live
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import os
import sqlite3
from datetime import datetime, timezone

from .live_odds_logger import EVENT_FIELDS, FIELDS as ODDS_FIELDS, LINEUP_FIELDS, STATS_FIELDS

DEFAULT_DB = os.path.join(os.path.expanduser(os.getenv("HYDRA_HOME", "~/.hydra_scout")), "live_odds.sqlite")
DEFAULT_OUT_DIR = "data/live"

# export name -> (sqlite table, exported columns, day-filter column, natural de-dup key)
EXPORTS: dict[str, tuple[str, list[str], str, tuple[str, ...]]] = {
    "odds": ("live_odds", ODDS_FIELDS, "ts_utc", ("event_id", "ts_utc")),
    "stats": ("live_stats", STATS_FIELDS, "ts_utc", ("event_id", "team_id", "ts_utc")),
    "events": ("key_events", EVENT_FIELDS, "ts_utc", ("event_id", "espn_event_id")),
    "closing": ("closing_odds", ODDS_FIELDS, "ts_utc", ("event_id",)),
    "lineups": ("lineups", LINEUP_FIELDS, "ts_utc", ("event_id", "team_id", "athlete_id")),
}


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    cur = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def query_day_rows(db: sqlite3.Connection, table: str, fields: list[str], day_col: str, day: str) -> list[dict]:
    """All rows of `table` whose `day_col` (a 'YYYY-MM-DDTHH:MM:SSZ' text column) falls on the UTC
    calendar day `day` ('YYYY-MM-DD'). Missing table (e.g. an old sqlite file predating this table) ->
    []  rather than an error, so export never fails on a partially-upgraded db."""
    if not _table_exists(db, table):
        return []
    cols = ", ".join(fields)
    cur = db.execute(f"SELECT {cols} FROM {table} WHERE substr({day_col}, 1, 10) = ? ORDER BY id", (day,))
    return [dict(zip(fields, r)) for r in cur.fetchall()]


def read_existing_csv_gz(path: str, fields: list[str]) -> list[dict]:
    if not os.path.exists(path):
        return []
    with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [{k: (row.get(k) or None) for k in fields} for row in reader]


def merge_dedupe(existing: list[dict], new: list[dict], key_fields: tuple[str, ...]) -> list[dict]:
    """Unions `existing` (already on disk, e.g. committed by an earlier run) with `new` (just read
    from sqlite), keeping the existing row whenever both share a key — the file already on disk is
    treated as canonical, `new` only ever adds rows under keys not already present. Existing order is
    preserved; new rows are appended in their (sqlite id, hence chronological) order."""
    seen = {tuple(str(r.get(k)) for k in key_fields) for r in existing}
    merged = list(existing)
    for r in new:
        key = tuple(str(r.get(k)) for k in key_fields)
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)
    return merged


def write_csv_gz(path: str, rows: list[dict], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k) for k in fields})
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        fh.write(buf.getvalue())


def export_day(day: str, db_path: str | None = None, out_dir: str | None = None) -> dict[str, int]:
    """Writes/updates {odds,stats,events,closing,lineups}.csv.gz under <out_dir>/<day>/ for one UTC day.
    Returns the post-merge row count for each. Safe to call repeatedly (including with no new data,
    or against a sqlite file that doesn't exist yet) — see module docstring."""
    db_path = db_path or DEFAULT_DB
    out_dir = out_dir or DEFAULT_OUT_DIR
    counts: dict[str, int] = {}
    if not os.path.exists(db_path):
        return {name: 0 for name in EXPORTS}
    db = sqlite3.connect(db_path)
    try:
        for name, (table, fields, day_col, key_fields) in EXPORTS.items():
            new_rows = query_day_rows(db, table, fields, day_col, day)
            out_path = os.path.join(out_dir, day, f"{name}.csv.gz")
            existing_rows = read_existing_csv_gz(out_path, fields)
            merged = merge_dedupe(existing_rows, new_rows, key_fields)
            if merged:
                write_csv_gz(out_path, merged, fields)
            counts[name] = len(merged)
    finally:
        db.close()
    return counts


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Export hydra_scout live-odds sqlite tables to gzipped daily CSVs.")
    p.add_argument("--day", default=None, help="UTC calendar day YYYY-MM-DD (default: today UTC)")
    p.add_argument("--db", default=None, help="sqlite path (default ~/.hydra_scout/live_odds.sqlite)")
    p.add_argument("--out-dir", default=None, help="output root (default data/live)")
    args = p.parse_args(argv)
    day = args.day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = args.out_dir or DEFAULT_OUT_DIR
    counts = export_day(day, db_path=args.db, out_dir=args.out_dir)
    for name, n in counts.items():
        print(f"[export] {os.path.join(out_dir, day, name + '.csv.gz')}: {n} row(s)", flush=True)


if __name__ == "__main__":
    main()
