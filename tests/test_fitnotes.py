import pytest

from garmindash import db, fitnotes

CSV_V1 = """Date,Exercise,Category,Weight (kgs),Reps,Distance,Distance Unit,Time,Comment
2026-09-01,Flat Barbell Bench Press,Chest,60.0,8,,,,
2026-09-01,Flat Barbell Bench Press,Chest,60.0,8,,,,
2026-09-01,Flat Barbell Bench Press,Chest,62.5,6,,,,last set hard
2026-09-01,Treadmill,Cardio,,,2.5,km,0:15:30,
2026-09-03,Pull Up,Back,0.0,10,,,,
"""

CSV_V2 = CSV_V1.replace("62.5,6,,,,last set hard", "62.5,7,,,,last set hard") + \
    "2026-09-05,Deadlift,Back,100.0,5,,,,\n"


def write(tmp_path, text, name="FitNotes_Export.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_import_and_columns(tmp_path, db_path):
    conn = db.connect(db_path)
    res = fitnotes.import_fitnotes(conn, write(tmp_path, CSV_V1))
    assert res.added == 5
    rows = conn.execute("SELECT * FROM workouts ORDER BY id").fetchall()
    assert [r["set_number"] for r in rows] == [1, 2, 3, 1, 1]
    assert (rows[0]["weight"], rows[0]["weight_unit"], rows[0]["reps"]) == (60.0, "kg", 8)
    assert rows[2]["comment"] == "last set hard"
    tread = rows[3]
    assert (tread["distance"], tread["distance_unit"], tread["time_seconds"]) == (2.5, "km", 930)
    assert tread["weight"] is None and tread["weight_unit"] is None
    assert rows[4]["weight"] == 0.0  # bodyweight


def test_reimport_newer_export_adds_only_new(tmp_path, db_path):
    conn = db.connect(db_path)
    fitnotes.import_fitnotes(conn, write(tmp_path, CSV_V1))
    res = fitnotes.import_fitnotes(conn, write(tmp_path, CSV_V2, "v2.csv"))
    assert (res.added, res.updated, res.unchanged) == (1, 1, 4)
    assert conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0] == 6
    res = fitnotes.import_fitnotes(conn, write(tmp_path, CSV_V2, "v2.csv"))
    assert (res.added, res.updated) == (0, 0)


def test_lbs_reordered_and_missing_columns(tmp_path, db_path):
    text = "﻿Exercise,Reps,Weight (lbs),Date\nSquat,5,225,2026-09-02\n"
    conn = db.connect(db_path)
    fitnotes.import_fitnotes(conn, write(tmp_path, text))
    r = conn.execute("SELECT * FROM workouts").fetchone()
    assert (r["exercise"], r["weight"], r["weight_unit"], r["reps"]) == ("Squat", 225.0, "lb", 5)
    assert r["category"] is None and r["comment"] is None and r["time_seconds"] is None


def test_separate_weight_unit_column(tmp_path):
    rows = fitnotes.parse_rows("Date,Exercise,Weight,Weight Unit\n2026-09-02,Curl,30,lbs\n")
    assert (rows[0]["weight"], rows[0]["weight_unit"]) == (30.0, "lb")


def test_date_format_option_and_error():
    text = "Date,Exercise,Reps\n02/09/2026,Curl,10\n"
    with pytest.raises(fitnotes.FitNotesError, match="--date-format"):
        fitnotes.parse_rows(text)
    assert fitnotes.parse_rows(text, "%d/%m/%Y")[0]["date"] == "2026-09-02"


def test_missing_required_column():
    with pytest.raises(fitnotes.FitNotesError, match="exercise"):
        fitnotes.parse_rows("Date,Reps\n2026-09-02,5\n")


@pytest.mark.parametrize("s,expected", [("1:02:03", 3723), ("02:03", 123), ("75", 75),
                                        ("", None), ("abc", None)])
def test_parse_time(s, expected):
    assert fitnotes.parse_time(s) == expected
