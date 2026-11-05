import pytest

from garmindash import corrections, db, fitnotes
from garmindash.cli import main
from test_fitnotes_backup import make_backup

SETS = [  # _id, exercise_id, date, metric_weight, reps, distance, duration_seconds, unit
    (10, 2, "2026-04-21", 0.0, 0, 1.25, 0, 2),   # logged as 1.25 m, meant miles
    (11, 1, "2026-04-17", 65.0, 8, 0, 0, 0),
]


@pytest.fixture
def imported(tmp_path, db_path):
    backup = make_backup(tmp_path / "b.fitnotes", SETS, comments=())
    fitnotes.import_fitnotes(db.connect(db_path), backup)
    return backup


def test_parse_day():
    assert corrections.parse_day("2026-04-21") == "2026-04-21"
    assert corrections.parse_day("4-21-2026") == "2026-04-21"
    assert corrections.parse_day("4/21/2026") == "2026-04-21"
    with pytest.raises(corrections.CorrectionError):
        corrections.parse_day("21st April")


def test_parse_assignment():
    assert corrections.parse_assignment("distance_unit=mi") == ("distance_unit", "mi")
    assert corrections.parse_assignment("reps=9") == ("reps", 9)
    assert corrections.parse_assignment("comment=") == ("comment", None)
    with pytest.raises(corrections.CorrectionError, match="can't correct"):
        corrections.parse_assignment("date=2026-01-01")
    with pytest.raises(corrections.CorrectionError, match="must be a int"):
        corrections.parse_assignment("reps=lots")


def test_correction_survives_reimport(imported, db_path, capsys):
    dbp = str(db_path)
    assert main(["correct-workout", "4-21-2026", "treadmill", "1",
                 "distance_unit=mi", "--note", "typo in app", "--db", dbp]) == 0
    assert "distance_unit: 'm' -> 'mi'" in capsys.readouterr().out
    conn = db.connect(db_path)
    q = "SELECT distance, distance_unit FROM workouts WHERE fitnotes_id = 10"
    assert tuple(conn.execute(q).fetchone()) == (1.25, "mi")
    res = fitnotes.import_fitnotes(conn, imported)
    assert (res.added, res.updated, res.unchanged) == (0, 0, 2)
    assert tuple(conn.execute(q).fetchone()) == (1.25, "mi")


def test_listing_and_errors(imported, db_path, capsys):
    dbp = str(db_path)
    assert main(["correct-workout", "2026-04-17", "Flat Barbell Bench Press", "--db", dbp]) == 0
    assert "set 1  65 kg  x8" in capsys.readouterr().out
    assert main(["correct-workout", "2026-04-17", "Flat Barbell Bench Press", "2",
                 "reps=9", "--db", dbp]) == 2
    assert "no set 2" in capsys.readouterr().err
    assert main(["correct-workout", "2026-04-17", "Flat Barbell Bench Press", "1",
                 "--db", dbp]) == 2
    assert "field=value" in capsys.readouterr().err
    assert main(["correct-workout", "2026-04-17", "Squat", "--db", dbp]) == 1


def test_csv_rows_cannot_be_corrected(tmp_path, db_path):
    csv = tmp_path / "e.csv"
    csv.write_text("Date,Exercise,Reps\n2026-04-17,Curl,10\n")
    conn = db.connect(db_path)
    fitnotes.import_fitnotes(conn, csv)
    with pytest.raises(corrections.CorrectionError, match="CSV"):
        corrections.add(conn, "2026-04-17", "Curl", 1, {"reps": 12})
