import pytest
from conftest import BREATHE, breathe_fit, monitoring_fit, run_fit
from garmindash import db, fit


def test_parse_run():
    act = fit.parse_activity(run_fit())
    assert act.start_utc == "2026-09-01T11:00:00Z"
    assert act.start_local == "2026-09-01T07:00:00"
    assert (act.sport, act.sub_sport) == ("running", "generic")
    assert act.duration_seconds == 1800
    assert round(act.distance_m, 1) == 5012.3
    assert (act.avg_hr, act.max_hr, act.calories) == (148, 171, 380)
    assert act.dev_fields == {}


def test_parse_breathe_developer_fields():
    act = fit.parse_activity(breathe_fit())
    assert (act.sport, act.sub_sport) == ("meditation", "breathing")
    vals = fit.breathe_values(act.dev_fields)
    assert {k: vals[k] for k in BREATHE} == BREATHE
    assert vals["extra_json"] is None


def test_breathe_names_tolerate_case_and_extra_fields():
    vals = fit.breathe_values({"HR Pre": 60.0, "Breaths": 40, "app_ver": 3})
    assert vals["hr_pre"] == 60.0 and vals["breaths"] == 40
    assert vals["extra_json"] == '{"app_ver": 3}'
    assert fit.breathe_values({"unrelated": 1}) is None


def test_monitoring_file_is_not_activity():
    try:
        fit.parse_activity(monitoring_fit())
    except fit.NotAnActivity:
        pass
    else:
        raise AssertionError("expected NotAnActivity")


def test_import_folder_is_idempotent(watch_folder, db_path):
    conn = db.connect(db_path)
    first = fit.import_fit_folder(conn, watch_folder)
    assert first.added == 2
    assert first.not_activity == 1
    assert len(first.errors) == 1 and "corrupt.fit" in first.errors[0]
    second = fit.import_fit_folder(conn, watch_folder)
    assert second.added == 0 and second.duplicates == 2
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2
    row = conn.execute("SELECT * FROM breathe_sessions").fetchone()
    assert row["pattern"] == "4-7-8" and row["minutes"] == 10
    assert row["start_utc"] == "2026-09-05T07:00:00Z"


@pytest.mark.filterwarnings("ignore:mismatching CRC")
def test_same_activity_different_bytes_dedupes_on_start_and_sport(db_path):
    conn = db.connect(db_path)
    a = run_fit()
    b = a[:-2] + b"\x00\x00"  # different hash (bad CRC), same content
    assert fit.import_fit_bytes(conn, a, "a.fit").added == 1
    res = fit.import_fit_bytes(conn, b, "b.fit")
    assert res.added == 0 and res.duplicates == 1


def test_deleting_activity_cascades_to_breathe(db_path):
    conn = db.connect(db_path)
    fit.import_fit_bytes(conn, breathe_fit(), "b.fit")
    conn.execute("DELETE FROM activities")
    assert conn.execute("SELECT COUNT(*) FROM breathe_sessions").fetchone()[0] == 0
