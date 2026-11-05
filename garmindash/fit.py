"""Parse activity FIT files and store them."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import fitdecode
from fitdecode.types import DevField

BREATHE_KEYS = ("pattern", "minutes", "breaths", "hr_pre", "hr_post",
                "rmssd_pre", "rmssd_post")

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)


class NotAnActivity(Exception):
    """FIT file is valid but not an activity (e.g. monitoring, settings)."""


@dataclass
class Activity:
    start_utc: str
    sport: str
    sub_sport: str | None = None
    start_local: str | None = None
    duration_seconds: float | None = None
    elapsed_seconds: float | None = None
    distance_m: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    calories: int | None = None
    dev_fields: dict = field(default_factory=dict)


def iso_utc(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(FIT_EPOCH.timestamp() + value, timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _enum_str(value) -> str | None:
    if value is None:
        return None
    return str(value)


def _num(value, cast=float):
    if value is None:
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def file_type(data: bytes) -> str | None:
    """Return the FIT file_id.type ('activity', 'monitoring_b', ...) or None."""
    with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.DISABLED,
                             error_handling=fitdecode.ErrorHandling.IGNORE) as reader:
        for frame in reader:
            if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "file_id":
                return _enum_str(frame.get_value("type", fallback=None))
    return None


def message_summary(data: bytes) -> dict[str, int]:
    """Count data messages by name (used by `inspect`)."""
    counts: dict[str, int] = {}
    with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.DISABLED,
                             error_handling=fitdecode.ErrorHandling.IGNORE) as reader:
        for frame in reader:
            if isinstance(frame, fitdecode.FitDataMessage):
                counts[frame.name] = counts.get(frame.name, 0) + 1
    return counts


def parse_activity(data: bytes) -> Activity:
    """Parse the first session of an activity FIT file.

    Raises NotAnActivity for non-activity files and fitdecode errors for
    corrupt ones.
    """
    ftype = None
    session = None
    lap_dev: dict = {}
    activity_msg = None
    with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.WARN,
                             error_handling=fitdecode.ErrorHandling.WARN) as reader:
        for frame in reader:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "file_id" and ftype is None:
                ftype = _enum_str(frame.get_value("type", fallback=None))
            elif frame.name == "session" and session is None:
                session = frame
            elif frame.name == "lap" and not lap_dev:
                lap_dev = _dev_fields(frame)
            elif frame.name == "activity" and activity_msg is None:
                activity_msg = frame
    if ftype != "activity":
        raise NotAnActivity(f"file type is {ftype!r}")
    if session is None:
        raise NotAnActivity("activity file has no session message")

    def get(name):
        return session.get_value(name, fallback=None)

    start = get("start_time") or get("timestamp")
    act = Activity(
        start_utc=iso_utc(start),
        sport=_enum_str(get("sport")) or "unknown",
        sub_sport=_enum_str(get("sub_sport")),
        duration_seconds=_num(get("total_timer_time")),
        elapsed_seconds=_num(get("total_elapsed_time")),
        distance_m=_num(get("total_distance")),
        avg_hr=_num(get("avg_heart_rate"), int),
        max_hr=_num(get("max_heart_rate"), int),
        calories=_num(get("total_calories"), int),
        dev_fields=_dev_fields(session) or lap_dev,
    )
    # Local wall-clock start = UTC start + (activity.local_timestamp - activity.timestamp)
    if activity_msg is not None and isinstance(start, datetime):
        ts = activity_msg.get_value("timestamp", fallback=None)
        local = activity_msg.get_value("local_timestamp", fallback=None)
        if isinstance(ts, datetime) and local is not None:
            if isinstance(local, (int, float)):
                local = datetime.fromtimestamp(FIT_EPOCH.timestamp() + local, timezone.utc)
            offset = local.replace(tzinfo=timezone.utc) - ts.astimezone(timezone.utc)
            act.start_local = (start.astimezone(timezone.utc) + offset).strftime(
                "%Y-%m-%dT%H:%M:%S")
    return act


def _dev_fields(frame) -> dict:
    out = {}
    for fd in frame.fields:
        if isinstance(fd.field, DevField) and fd.field.name:
            value = fd.value
            if isinstance(value, float):
                value = round(value, 3)
            elif isinstance(value, tuple):
                value = list(value)
            out[fd.field.name] = value
    return out


def _norm_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def breathe_values(dev: dict) -> dict | None:
    """Map developer fields to breathe_sessions columns, tolerating case and
    separator differences ('HR Pre', 'hr-pre' -> hr_pre). None if none match."""
    normalised = {_norm_key(k): v for k, v in dev.items()}
    found = {k: normalised[k] for k in BREATHE_KEYS if k in normalised}
    if not found:
        return None
    extra = {k: v for k, v in dev.items() if _norm_key(k) not in BREATHE_KEYS}
    found["extra_json"] = json.dumps(extra, sort_keys=True) if extra else None
    if isinstance(found.get("pattern"), str):
        found["pattern"] = found["pattern"].rstrip("\x00")
    return found


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_activity(conn: sqlite3.Connection, act: Activity, source: str,
                   digest: str, garmin_id: int | None = None) -> bool:
    """Insert one activity (and its breathe row). Returns False if it was
    already present (same file hash, or same start time + sport)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO activities
           (garmin_activity_id, start_utc, start_local, sport, sub_sport,
            duration_seconds, elapsed_seconds, distance_m, avg_hr, max_hr,
            calories, source_file, file_sha256)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (garmin_id, act.start_utc, act.start_local, act.sport, act.sub_sport,
         act.duration_seconds, act.elapsed_seconds, act.distance_m, act.avg_hr,
         act.max_hr, act.calories, source, digest))
    if cur.rowcount == 0:
        if garmin_id is not None:  # fill the Connect id if the USB copy came first
            conn.execute(
                """UPDATE activities SET garmin_activity_id = ?
                   WHERE garmin_activity_id IS NULL AND start_utc = ? AND sport = ?""",
                (garmin_id, act.start_utc, act.sport))
        return False
    breathe = breathe_values(act.dev_fields)
    if breathe is not None:
        conn.execute(
            """INSERT OR IGNORE INTO breathe_sessions
               (activity_id, start_utc, pattern, minutes, breaths, hr_pre,
                hr_post, rmssd_pre, rmssd_post, extra_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cur.lastrowid, act.start_utc, breathe.get("pattern"),
             breathe.get("minutes"), breathe.get("breaths"), breathe.get("hr_pre"),
             breathe.get("hr_post"), breathe.get("rmssd_pre"),
             breathe.get("rmssd_post"), breathe["extra_json"]))
    return True


@dataclass
class FitImportResult:
    added: int = 0
    duplicates: int = 0
    not_activity: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "FitImportResult") -> None:
        self.added += other.added
        self.duplicates += other.duplicates
        self.not_activity += other.not_activity
        self.errors.extend(other.errors)


def import_fit_bytes(conn, data: bytes, source: str,
                     garmin_id: int | None = None) -> FitImportResult:
    res = FitImportResult()
    digest = sha256(data)
    if conn.execute("SELECT 1 FROM activities WHERE file_sha256 = ?", (digest,)).fetchone():
        if garmin_id is not None:  # USB copy imported first: record the Connect id
            conn.execute("UPDATE activities SET garmin_activity_id = ? "
                         "WHERE file_sha256 = ? AND garmin_activity_id IS NULL",
                         (garmin_id, digest))
        res.duplicates += 1
        return res
    try:
        act = parse_activity(data)
    except NotAnActivity:
        res.not_activity += 1
        return res
    except Exception as exc:  # corrupt / truncated file: report, keep going
        res.errors.append(f"{source}: {exc}")
        return res
    if store_activity(conn, act, source, digest, garmin_id):
        res.added += 1
    else:
        res.duplicates += 1
    return res


def import_fit_folder(conn, folder: str | Path) -> FitImportResult:
    """Import every *.fit under `folder` (recursive, case-insensitive)."""
    folder = Path(folder)
    res = FitImportResult()
    paths = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".fit")
    for path in paths:
        res.merge(import_fit_bytes(conn, path.read_bytes(), str(path.relative_to(folder))))
    conn.commit()
    return res
