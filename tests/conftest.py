"""Synthetic fixtures. All data here is invented; no real personal data."""
from __future__ import annotations

import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fitwriter import activity_fit  # noqa: E402

RUNNING, WALKING, MEDITATION = 1, 11, 67
GENERIC, BREATHING = 0, 62

BREATHE = dict(pattern="4-7-8", minutes=10, breaths=55, hr_pre=64.5,
               hr_post=58.0, rmssd_pre=35.25, rmssd_post=48.75)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def run_fit():
    return activity_fit(utc(2026, 9, 1, 11, 0), RUNNING, GENERIC, 1800,
                        distance_m=5012.3, avg_hr=148, max_hr=171, calories=380,
                        utc_offset_s=-4 * 3600)


def walk_fit():
    return activity_fit(utc(2026, 9, 2, 22, 30), WALKING, GENERIC, 2400,
                        distance_m=3100.0, avg_hr=98, max_hr=115, calories=150)


def breathe_fit(start=None):
    return activity_fit(start or utc(2026, 9, 3, 12, 0), MEDITATION, BREATHING, 600,
                        avg_hr=61, max_hr=70, calories=15, breathe=BREATHE)


def monitoring_fit():
    return activity_fit(utc(2026, 9, 3), 0, 0, 0, file_type=32)  # monitoring_b


SLEEP_JSON = [
    {   # current-style record
        "sleepStartTimestampGMT": "2026-09-01T03:10:00.0",
        "sleepEndTimestampGMT": "2026-09-01T10:40:00.0",
        "calendarDate": "2026-09-01",
        "sleepWindowConfirmationType": "ENHANCED_CONFIRMED_FINAL",
        "deepSleepSeconds": 5400, "lightSleepSeconds": 14400,
        "remSleepSeconds": 6000, "awakeSleepSeconds": 1200,
        "unmeasurableSeconds": 0, "averageRespiration": 14.0,
    },
    {   # epoch-millisecond timestamps, no REM key
        "sleepStartTimestamp": 1788318000000, "sleepEndTimestamp": 1788343200000,
        "calendarDate": "2026-09-02",
        "deepSleepSeconds": 3600, "lightSleepSeconds": 18000, "awakeSleepSeconds": 600,
    },
    {"calendarDate": "2026-09-03", "sleepWindowConfirmationType": "UNCONFIRMED"},
]

UDS_JSON = [
    {   # current style: nested stress + body battery
        "calendarDate": "2026-09-01", "totalSteps": 9876, "restingHeartRate": 52,
        "allDayStress": {"aggregatorList": [
            {"type": "TOTAL", "averageStressLevel": 31, "maxStressLevel": 88},
            {"type": "AWAKE", "averageStressLevel": 40, "maxStressLevel": 88}]},
        "bodyBattery": {"chargedValue": 60, "drainedValue": 55, "bodyBatteryStatList": [
            {"bodyBatteryStatType": "HIGHEST", "statsValue": 95},
            {"bodyBatteryStatType": "LOWEST", "statsValue": 20}]},
    },
    {   # older style: calendarDate object, flat keys, -1 = no data
        "calendarDate": {"date": "Sep 2, 2026 12:00:00 AM"}, "totalSteps": 4321,
        "currentDayRestingHeartRate": 55, "averageStressLevel": -1,
        "maxStressLevel": 70, "bodyBatteryHighestValue": 80, "bodyBatteryLowestValue": 15,
        "bodyBatteryChargedValue": 50, "bodyBatteryDrainedValue": 65,
    },
]


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def build_export(path: Path) -> Path:
    uploads = _zip_bytes({
        "someone_1111111111.fit": run_fit(),
        "someone_2222222222.fit": walk_fit(),
        "someone_3333333333.fit": breathe_fit(),
        "someone_4444444444.fit": monitoring_fit(),
        "someone_5555555555.tcx": b"<TrainingCenterDatabase/>",
    })
    root = "DI_CONNECT/"
    entries = {
        root + "DI-Connect-Wellness/2026-08-01_2026-11-01_12345_sleepData.json":
            json.dumps(SLEEP_JSON).encode(),
        root + "DI-Connect-Aggregator/UDSFile_2026-08-01_2026-11-01.json":
            json.dumps(UDS_JSON).encode(),
        root + "DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip": uploads,
        root + "DI-Connect-Fitness/someone_0_summarizedActivities.json":
            json.dumps([{"summarizedActivitiesExport": []}]).encode(),
        root + "DI-Connect-Wellness/12345_mysteryMetric.json":
            json.dumps([{"foo": 1, "barValue": 2}]).encode(),
        root + "DI-Connect-Wellness/2026_broken_sleepData.json":
            json.dumps([{"unexpected": True}]).encode(),
        "README.txt": b"export readme",
    }
    path.write_bytes(_zip_bytes(entries))
    return path


@pytest.fixture
def export_zip(tmp_path) -> Path:
    return build_export(tmp_path / "export.zip")


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "garmin.db"


@pytest.fixture
def watch_folder(tmp_path) -> Path:
    act = tmp_path / "GARMIN" / "Activity"
    act.mkdir(parents=True)
    (act / "2026-09-01-11-00-00.fit").write_bytes(run_fit())
    (act / "2026-09-05-07-00-00.FIT").write_bytes(
        breathe_fit(utc(2026, 9, 5, 7, 0)))
    (act / "corrupt.fit").write_bytes(b"not a fit file at all")
    mon = tmp_path / "GARMIN" / "Monitor"
    mon.mkdir()
    (mon / "M1.FIT").write_bytes(monitoring_fit())
    return tmp_path / "GARMIN"
