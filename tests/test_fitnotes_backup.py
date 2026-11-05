"""Synthetic .fitnotes backup (SQLite) built from the real app's table layout."""
import sqlite3

import pytest

from garmindash import db, fitnotes
from garmindash.cli import main

SCHEMA = """
CREATE TABLE android_metadata (locale TEXT);
CREATE TABLE Category (_id INTEGER PRIMARY KEY, name TEXT, colour INTEGER, sort_order INTEGER);
CREATE TABLE exercise (_id INTEGER PRIMARY KEY, name TEXT, category_id INTEGER,
    exercise_type_id INTEGER, notes TEXT, weight_increment INTEGER, default_graph_id INTEGER,
    default_rest_time INTEGER, weight_unit_id INTEGER, is_favourite INTEGER);
CREATE TABLE training_log (_id INTEGER PRIMARY KEY, exercise_id INTEGER, date TEXT,
    metric_weight REAL, reps INTEGER, unit INTEGER, routine_section_exercise_set_id INTEGER,
    timer_auto_start INTEGER, is_personal_record INTEGER, is_personal_record_first INTEGER,
    is_complete INTEGER, is_pending_update INTEGER, distance INTEGER, duration_seconds INTEGER);
CREATE TABLE Comment (_id INTEGER PRIMARY KEY, date TEXT, owner_type_id INTEGER,
    owner_id INTEGER, comment TEXT);
"""

SETS = [  # _id, exercise_id, date, metric_weight, reps, distance, duration_seconds, unit
    (1, 1, "2026-04-28", 60.0, 8, 0, 0, 0),
    (2, 1, "2026-04-28", 60.0, 8, 0, 0, 0),
    (3, 1, "2026-04-28", 62.5, 6, 0, 0, 0),
    (4, 2, "2026-04-28", 0.0, 0, 2.0, 930, 5),   # 2.0 mi
    (5, 3, "2026-04-30", 0.0, 10, 0, 0, 0),
]


def make_backup(path, sets=SETS, comments=((3, "2026-04-28", "last set hard"),)):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO Category (_id, name) VALUES (?, ?)",
                     [(1, "Chest"), (2, "Cardio"), (3, "Back")])
    conn.executemany("INSERT INTO exercise (_id, name, category_id) VALUES (?, ?, ?)",
                     [(1, "Flat Barbell Bench Press", 1), (2, "Treadmill", 2), (3, "Pull Up", 3)])
    conn.executemany(
        "INSERT INTO training_log (_id, exercise_id, date, metric_weight, reps, distance, "
        "duration_seconds, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", sets)
    conn.executemany("INSERT INTO Comment (owner_type_id, owner_id, date, comment) "
                     "VALUES (1, ?, ?, ?)", comments)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def backup(tmp_path):
    return make_backup(tmp_path / "FitNotes_Backup_2026_05_01.fitnotes")


def test_detected_by_content(backup, tmp_path):
    assert fitnotes.is_backup(backup)
    csv = tmp_path / "x.csv"
    csv.write_text("Date,Exercise\n")
    assert not fitnotes.is_backup(csv)


def test_import_backup(backup, db_path):
    conn = db.connect(db_path)
    res = fitnotes.import_fitnotes(conn, backup)
    assert res.added == 5
    rows = conn.execute("SELECT * FROM workouts ORDER BY fitnotes_id").fetchall()
    assert [r["set_number"] for r in rows] == [1, 2, 3, 1, 1]
    bench = rows[2]
    assert (bench["exercise"], bench["category"], bench["weight"], bench["weight_unit"],
            bench["reps"], bench["comment"]) == (
        "Flat Barbell Bench Press", "Chest", 62.5, "kg", 6, "last set hard")
    tread = rows[3]
    assert (tread["distance"], tread["distance_unit"], tread["time_seconds"], tread["reps"]) == (
        2.0, "mi", 930, None)
    assert rows[0]["distance"] is None and rows[0]["distance_unit"] is None
    assert rows[4]["weight"] == 0.0 and rows[4]["reps"] == 10  # bodyweight pull-up


def test_backup_is_not_modified(backup, db_path):
    before = backup.read_bytes()
    fitnotes.import_fitnotes(db.connect(db_path), backup)
    assert backup.read_bytes() == before


def test_newer_backup_adds_new_updates_edited_reports_deleted(backup, tmp_path, db_path):
    conn = db.connect(db_path)
    fitnotes.import_fitnotes(conn, backup)
    newer = [s for s in SETS if s[0] != 2]                     # set 2 deleted
    newer = [(3, 1, "2026-04-28", 62.5, 7, 0, 0, 0) if s[0] == 3 else s for s in newer]  # edited
    newer.append((6, 1, "2026-05-01", 65.0, 5, 0, 0, 0))         # new
    res = fitnotes.import_fitnotes(conn, make_backup(tmp_path / "newer.fitnotes", newer))
    assert (res.added, res.updated, res.missing_from_source) == (1, 1, 1)
    # set 3 moved from position 3 to 2 after the deletion, so both its reps and set_number changed
    row = conn.execute("SELECT reps, set_number FROM workouts WHERE fitnotes_id = 3").fetchone()
    assert tuple(row) == (7, 2)
    again = fitnotes.import_fitnotes(conn, tmp_path / "newer.fitnotes")
    assert (again.added, again.updated) == (0, 0)


def test_csv_then_backup_does_not_duplicate(backup, tmp_path, db_path):
    conn = db.connect(db_path)
    csv = tmp_path / "e.csv"
    csv.write_text("Date,Exercise,Category,Weight (kgs),Reps\n"
                   "2026-04-28,Flat Barbell Bench Press,Chest,60.0,8\n"
                   "2026-04-28,Flat Barbell Bench Press,Chest,60.0,8\n")
    fitnotes.import_fitnotes(conn, csv)
    res = fitnotes.import_fitnotes(conn, backup)
    assert res.added == 3 and res.unchanged == 2
    assert conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM workouts WHERE fitnotes_id IS NULL").fetchone()[0] == 0
    # and a CSV re-import afterwards leaves the backup's rows alone
    res = fitnotes.import_fitnotes(conn, csv)
    assert (res.added, res.updated) == (0, 0)


def test_cli_backup_and_bad_sqlite(backup, tmp_path, capsys):
    dbp = str(tmp_path / "g.db")
    assert main(["import-fitnotes", str(backup), "--db", dbp]) == 0
    assert "workouts: 5 added" in capsys.readouterr().out
    other = tmp_path / "other.fitnotes"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()
    assert main(["import-fitnotes", str(other), "--db", dbp]) == 2
    assert "not a FitNotes backup" in capsys.readouterr().err


def test_distance_unit_codes(tmp_path, db_path, capsys):
    sets = [(1, 2, "2026-04-06", 0.0, 0, 50.0, 600, 2),    # 50 m
            (2, 2, "2026-04-11", 0.0, 0, 2.0, 0, 5),       # 2 mi
            (3, 2, "2026-04-12", 0.0, 0, 3.0, 0, 9)]       # unmapped code
    path = make_backup(tmp_path / "u.fitnotes", sets, comments=())
    dbp = str(db_path)
    assert main(["import-fitnotes", str(path), "--db", dbp]) == 0
    out = capsys.readouterr().out
    assert "1 sets have distance in an unmapped unit, stored as 'fitnotes_unit_9'" in out
    conn = db.connect(db_path)
    units = [tuple(r) for r in conn.execute(
        "SELECT distance, distance_unit FROM workouts ORDER BY fitnotes_id")]
    assert units == [(50.0, "m"), (2.0, "mi"), (3.0, "fitnotes_unit_9")]
