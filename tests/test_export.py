import json
import zipfile

from conftest import breathe_fit, run_fit
from garmindash import db, export, fit


def test_classify():
    assert export.classify("DI_CONNECT/DI-Connect-Wellness/a_b_1_sleepData.json") == "sleep"
    assert export.classify("x/UDSFile_2020-01-01_2020-04-01.json") == "daily"
    assert export.classify("x/UploadedFiles_0-_Part1.zip") == "uploaded_zip"
    assert export.classify("x/y.FIT") == "fit"
    assert export.classify("x/someone_summarizedActivities.json") == "known"
    assert export.classify("x/whatever.json") is None


def test_dates_and_timestamps():
    assert export.to_date("2026-09-01") == "2026-09-01"
    assert export.to_date({"date": "Sep 2, 2026 12:00:00 AM"}) == "2026-09-02"
    assert export.iso(export.to_datetime("2026-09-01T03:10:00.0")) == "2026-09-01T03:10:00Z"
    assert export.iso(export.to_datetime(1788318000000)) == "2026-09-02T03:00:00Z"
    assert export.to_datetime("garbage") is None


def test_import_export(export_zip, db_path):
    conn = db.connect(db_path)
    res = export.process_export(export_zip, conn)
    assert (res.sleep, res.daily) == (2, 2)
    assert res.activities.added == 3 and res.activities.not_activity == 1

    s1, s2 = conn.execute("SELECT * FROM sleep ORDER BY night_date").fetchall()
    assert dict(s1) | {"source_file": None} == {
        "night_date": "2026-09-01", "start_utc": "2026-09-01T03:10:00Z",
        "end_utc": "2026-09-01T10:40:00Z", "total_sleep_seconds": 25800,
        "deep_seconds": 5400, "light_seconds": 14400, "rem_seconds": 6000,
        "awake_seconds": 1200, "source_file": None}
    assert s2["start_utc"] == "2026-09-02T03:00:00Z"
    assert s2["rem_seconds"] is None and s2["total_sleep_seconds"] == 21600

    d1, d2 = conn.execute("SELECT * FROM daily ORDER BY date").fetchall()
    assert (d1["resting_hr"], d1["avg_stress"], d1["max_stress"], d1["steps"]) == (52, 31, 88, 9876)
    assert (d1["body_battery_high"], d1["body_battery_low"],
            d1["body_battery_charged"], d1["body_battery_drained"]) == (95, 20, 60, 55)
    assert d2["date"] == "2026-09-02" and d2["resting_hr"] == 55
    assert d2["avg_stress"] is None and d2["max_stress"] == 70
    assert d2["body_battery_high"] == 80 and d2["body_battery_drained"] == 65

    acts = conn.execute("SELECT sport, sub_sport, garmin_activity_id FROM activities "
                        "ORDER BY start_utc").fetchall()
    assert [tuple(a) for a in acts] == [("running", "generic", 1111111111),
                                       ("walking", "generic", 2222222222),
                                       ("meditation", "breathing", 3333333333)]
    b = conn.execute("SELECT * FROM breathe_sessions").fetchone()
    assert (b["breaths"], b["rmssd_post"]) == (55, 48.75)


def test_reimport_export_adds_nothing(export_zip, db_path):
    conn = db.connect(db_path)
    export.process_export(export_zip, conn)
    res = export.process_export(export_zip, conn)
    assert res.activities.added == 0 and res.activities.duplicates == 3
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM sleep").fetchone()[0] == 2


def test_usb_then_export_does_not_duplicate_and_fills_garmin_id(export_zip, db_path):
    conn = db.connect(db_path)
    fit.import_fit_bytes(conn, run_fit(), "GARMIN/Activity/x.fit")
    export.process_export(export_zip, conn)
    rows = conn.execute("SELECT garmin_activity_id FROM activities WHERE sport='running'").fetchall()
    assert [r[0] for r in rows] == [1111111111]


def test_overlapping_daily_files_do_not_null_out_values(tmp_path, db_path):
    zpath = tmp_path / "e.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("A/UDSFile_1.json", json.dumps(
            [{"calendarDate": "2026-01-01", "totalSteps": 100, "restingHeartRate": 50}]))
        zf.writestr("A/UDSFile_2.json", json.dumps(
            [{"calendarDate": "2026-01-01", "totalSteps": 200}]))
    conn = db.connect(db_path)
    export.process_export(zpath, conn)
    row = conn.execute("SELECT steps, resting_hr FROM daily").fetchone()
    assert tuple(row) == (200, 50)


def test_wrapped_json_and_bare_fit_in_outer_zip(tmp_path, db_path):
    zpath = tmp_path / "e.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("x/sleepData.json", json.dumps({"sleepList": [
            {"calendarDate": "2026-01-02", "deepSleepSeconds": 10}]}))
        zf.writestr("x/loose_9876543210.fit", breathe_fit())
    conn = db.connect(db_path)
    res = export.process_export(zpath, conn)
    assert res.sleep == 1 and res.activities.added == 1
