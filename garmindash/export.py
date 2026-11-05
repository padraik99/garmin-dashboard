"""Garmin Connect account export ("Export your data") importer.

The real layout varies by account and export date, so files are matched by
name pattern anywhere in the archive, and every field is looked up under
several candidate keys. `inspect` uses the same code in dry-run mode, so what
it reports is exactly what `import-export` would do.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath

from garmindash import fit as fitmod

# ---------------------------------------------------------------- classification

RECOGNISED = [  # (kind, regex on the file's base name, case-insensitive)
    ("sleep", r".*sleepdata.*\.json$"),
    ("daily", r"^udsfile.*\.json$"),
    ("uploaded_zip", r"^uploadedfiles.*\.zip$"),
    ("fit", r".*\.fit$"),
]

# Seen in Garmin exports but not imported (yet). Matched files are listed as
# "known" by inspect rather than flagged.
KNOWN_UNUSED = [
    r".*summarizedactivities.*\.json$", r".*personalrecord.*\.json$",
    r".*healthstatusdata.*\.json$", r".*hydration.*\.json$",
    r".*trainingreadiness.*\.json$", r".*trainingstatus.*\.json$",
    r".*fitnessagedata.*\.json$", r".*maxmetdata.*\.json$",
    r".*racepredictions.*\.json$", r".*heartratezones.*\.json$",
    r".*userbiometrics.*\.json$", r".*user_?profile.*\.json$",
    r".*user_?settings.*\.json$", r".*devices?.*\.json$", r".*gear.*\.json$",
    r".*goal.*\.json$", r".*badge.*\.json$", r".*challenge.*\.json$",
    r".*courses?.*\.json$", r".*workout.*\.json$", r".*menstrual.*\.json$",
    r".*\.(tcx|gpx|csv|txt|html?|pdf|png|jpe?g)$",
]


def classify(name: str) -> str | None:
    base = PurePosixPath(name).name.lower()
    for kind, pattern in RECOGNISED:
        if re.match(pattern, base):
            return kind
    for pattern in KNOWN_UNUSED:
        if re.match(pattern, base):
            return "known"
    return None


# ---------------------------------------------------------------- value helpers

def first(d: dict, *keys):
    """First non-None value among candidate keys (case-insensitive)."""
    lower = {k.lower(): v for k, v in d.items()} if isinstance(d, dict) else {}
    for key in keys:
        v = lower.get(key.lower())
        if v is not None:
            return v
    return None


def to_int(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def non_negative(v):
    """Garmin uses -1 / -2 for 'not enough data'."""
    v = to_int(v)
    return v if v is not None and v >= 0 else None


_DT_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
               "%b %d, %Y %I:%M:%S %p", "%Y-%m-%d")


def to_datetime(v) -> datetime | None:
    """Parse Garmin's assorted timestamp shapes; naive values are taken as UTC."""
    if v is None:
        return None
    if isinstance(v, dict):
        return to_datetime(first(v, "date", "value"))
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        secs = v / 1000 if v > 1e11 else v  # epoch ms vs s
        return datetime.fromtimestamp(secs, timezone.utc)
    if isinstance(v, str):
        s = v.strip()
        if s.endswith("Z"):
            s = s[:-1]
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            dt = None
            for fmt in _DT_FORMATS:
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def to_date(v) -> str | None:
    dt = to_datetime(v)
    return dt.strftime("%Y-%m-%d") if dt else None


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def records(doc) -> list[dict]:
    """Garmin JSON is usually a list of dicts, sometimes wrapped in a dict."""
    if isinstance(doc, list):
        out = []
        for item in doc:
            if isinstance(item, dict):
                out.append(item)
        return out
    if isinstance(doc, dict):
        for v in doc.values():
            if isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
                return v
        return [doc]
    return []


def top_level_keys(doc) -> list[str]:
    if isinstance(doc, dict):
        return sorted(doc.keys())
    if isinstance(doc, list):
        keys: set[str] = set()
        for item in doc[:50]:
            if isinstance(item, dict):
                keys.update(item.keys())
        return sorted(keys)
    return []


# ---------------------------------------------------------------- parsers

def parse_sleep(rec: dict) -> dict | None:
    stages = {
        "deep_seconds": to_int(first(rec, "deepSleepSeconds", "deepSleepDuration")),
        "light_seconds": to_int(first(rec, "lightSleepSeconds", "lightSleepDuration")),
        "rem_seconds": to_int(first(rec, "remSleepSeconds", "remSleepDuration")),
        "awake_seconds": to_int(first(rec, "awakeSleepSeconds", "awakeDuration",
                                      "awakeSleepDuration")),
    }
    start = to_datetime(first(rec, "sleepStartTimestampGMT", "sleepStartTimestamp",
                              "sleepStartTimestampGmt", "startTimeGMT"))
    end = to_datetime(first(rec, "sleepEndTimestampGMT", "sleepEndTimestamp",
                            "sleepEndTimestampGmt", "endTimeGMT"))
    if all(v is None for v in stages.values()) and start is None:
        return None
    night = to_date(first(rec, "calendarDate", "sleepDate")) or (
        end.strftime("%Y-%m-%d") if end else None)
    if night is None:
        return None
    asleep = [stages[k] for k in ("deep_seconds", "light_seconds", "rem_seconds")]
    total = to_int(first(rec, "sleepTimeSeconds", "totalSleepSeconds"))
    if total is None and any(v is not None for v in asleep):
        total = sum(v or 0 for v in asleep)
    return {"night_date": night, "start_utc": iso(start), "end_utc": iso(end),
            "total_sleep_seconds": total, **stages}


def _stress_total(rec: dict) -> tuple:
    stress = first(rec, "allDayStress")
    if isinstance(stress, dict):
        for agg in stress.get("aggregatorList") or []:
            if isinstance(agg, dict) and str(agg.get("type", "")).upper() == "TOTAL":
                return (non_negative(first(agg, "averageStressLevel")),
                        non_negative(first(agg, "maxStressLevel")))
    return (non_negative(first(rec, "averageStressLevel", "avgStressLevel")),
            non_negative(first(rec, "maxStressLevel")))


def _body_battery(rec: dict) -> dict:
    out = {
        "body_battery_high": to_int(first(rec, "bodyBatteryHighestValue")),
        "body_battery_low": to_int(first(rec, "bodyBatteryLowestValue")),
        "body_battery_charged": to_int(first(rec, "bodyBatteryChargedValue")),
        "body_battery_drained": to_int(first(rec, "bodyBatteryDrainedValue")),
    }
    bb = first(rec, "bodyBattery")
    if isinstance(bb, dict):
        for stat in bb.get("bodyBatteryStatList") or []:
            if not isinstance(stat, dict):
                continue
            kind = str(first(stat, "bodyBatteryStatType") or "").upper()
            value = to_int(first(stat, "statsValue", "value"))
            if kind == "HIGHEST" and out["body_battery_high"] is None:
                out["body_battery_high"] = value
            elif kind == "LOWEST" and out["body_battery_low"] is None:
                out["body_battery_low"] = value
        if out["body_battery_charged"] is None:
            out["body_battery_charged"] = to_int(first(bb, "chargedValue"))
        if out["body_battery_drained"] is None:
            out["body_battery_drained"] = to_int(first(bb, "drainedValue"))
    return out


def parse_daily(rec: dict) -> dict | None:
    date = to_date(first(rec, "calendarDate", "summaryDate", "date"))
    if date is None:
        return None
    avg_stress, max_stress = _stress_total(rec)
    row = {
        "date": date,
        "resting_hr": to_int(first(rec, "restingHeartRate", "currentDayRestingHeartRate",
                                   "restingHeartRateValue")),
        "avg_stress": avg_stress,
        "max_stress": max_stress,
        **_body_battery(rec),
        "steps": to_int(first(rec, "totalSteps", "steps")),
    }
    if all(v is None for k, v in row.items() if k != "date"):
        return None
    return row


def upsert(conn: sqlite3.Connection, table: str, key: str, row: dict) -> None:
    """Insert, or fill/overwrite existing columns with non-null new values."""
    cols = list(row)
    updates = ", ".join(f"{c} = COALESCE(excluded.{c}, {table}.{c})"
                        for c in cols if c != key)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}",
        [row[c] for c in cols])


# ---------------------------------------------------------------- driver

@dataclass
class FileReport:
    path: str
    kind: str | None          # sleep / daily / uploaded_zip / fit / known / None
    status: str               # used / known / unrecognised / skipped / error
    detail: str = ""
    children: list["FileReport"] = field(default_factory=list)


@dataclass
class ExportResult:
    files: list[FileReport] = field(default_factory=list)
    sleep: int = 0
    daily: int = 0
    activities: fitmod.FitImportResult = field(default_factory=fitmod.FitImportResult)


def garmin_id_from_name(name: str) -> int | None:
    runs = re.findall(r"\d{6,}", PurePosixPath(name).stem)
    return int(runs[-1]) if runs else None


def _json_file(zf, info, kind, conn, result) -> FileReport:
    rep = FileReport(info.filename, kind, "used")
    try:
        doc = json.loads(zf.read(info).decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        rep.status, rep.detail = "error", f"not valid JSON: {exc}"
        return rep
    parser, table, key = ((parse_sleep, "sleep", "night_date") if kind == "sleep"
                          else (parse_daily, "daily", "date"))
    recs = records(doc)
    rows = [r for r in (parser(rec) for rec in recs) if r]
    if not rows:
        rep.status = "unrecognised"
        rep.detail = (f"name matched {kind} but 0 of {len(recs)} records parsed; "
                      f"keys: {', '.join(top_level_keys(doc)) or '(none)'}")
        return rep
    if conn is not None:
        for row in rows:
            row["source_file"] = PurePosixPath(info.filename).name
            upsert(conn, table, key, row)
    setattr(result, kind, getattr(result, kind) + len(rows))
    rep.detail = f"{len(rows)} of {len(recs)} records parsed"
    return rep


def _fit_entry(data: bytes, name: str, conn, result) -> FileReport:
    gid = garmin_id_from_name(name)
    if conn is not None:
        res = fitmod.import_fit_bytes(conn, data, name, gid)
        result.activities.merge(res)
        if res.errors:
            return FileReport(name, "fit", "error", res.errors[0])
        if res.not_activity:
            return _non_activity(data, name)
        return FileReport(name, "fit", "used", "added" if res.added else "already in db")
    try:
        act = fitmod.parse_activity(data)
    except fitmod.NotAnActivity:
        result.activities.not_activity += 1
        return _non_activity(data, name)
    except Exception as exc:
        result.activities.errors.append(f"{name}: {exc}")
        return FileReport(name, "fit", "error", str(exc))
    result.activities.added += 1
    extra = " +breathe fields" if fitmod.breathe_values(act.dev_fields) else ""
    return FileReport(name, "fit", "used", f"{act.sport}/{act.sub_sport} {act.start_utc}{extra}")


def _non_activity(data: bytes, name: str) -> FileReport:
    try:
        ftype = fitmod.file_type(data)
        msgs = fitmod.message_summary(data)
        detail = f"FIT type={ftype}; messages: " + ", ".join(
            f"{k}x{v}" for k, v in sorted(msgs.items()))
    except Exception as exc:
        detail = f"unreadable FIT: {exc}"
    return FileReport(name, "fit", "unrecognised", detail)


def _uploaded_zip(zf, info, conn, result) -> FileReport:
    rep = FileReport(info.filename, "uploaded_zip", "used")
    with tempfile.TemporaryFile() as tmp:
        with zf.open(info) as src:
            shutil.copyfileobj(src, tmp)
        tmp.seek(0)
        try:
            inner = zipfile.ZipFile(tmp)
        except zipfile.BadZipFile as exc:
            rep.status, rep.detail = "error", str(exc)
            return rep
        counts: dict[str, int] = {}
        with inner:
            for sub in inner.infolist():
                if sub.is_dir():
                    continue
                ext = PurePosixPath(sub.filename).suffix.lower() or "(none)"
                counts[ext] = counts.get(ext, 0) + 1
                if ext == ".fit":
                    child = _fit_entry(inner.read(sub), sub.filename, conn, result)
                else:
                    known = classify(sub.filename) == "known"
                    child = FileReport(sub.filename, None, "skipped" if known else "unrecognised",
                                       "" if known else "unexpected file type in uploads zip")
                rep.children.append(child)
    rep.detail = ", ".join(f"{n} {ext}" for ext, n in sorted(counts.items()))
    return rep


def process_export(zip_path, conn: sqlite3.Connection | None = None) -> ExportResult:
    """Import an export ZIP into `conn`, or dry-run when conn is None."""
    result = ExportResult()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            kind = classify(info.filename)
            if kind in ("sleep", "daily"):
                rep = _json_file(zf, info, kind, conn, result)
            elif kind == "uploaded_zip":
                rep = _uploaded_zip(zf, info, conn, result)
            elif kind == "fit":
                rep = _fit_entry(zf.read(info), info.filename, conn, result)
            elif kind == "known":
                rep = FileReport(info.filename, kind, "known")
            elif info.filename.lower().endswith(".json"):
                try:
                    keys = top_level_keys(json.loads(zf.read(info).decode("utf-8-sig")))
                    detail = "keys: " + (", ".join(keys) or "(none)")
                except (ValueError, UnicodeDecodeError) as exc:
                    detail = f"not valid JSON: {exc}"
                rep = FileReport(info.filename, None, "unrecognised", detail)
            elif info.filename.lower().endswith(".zip"):
                rep = FileReport(info.filename, None, "unrecognised", "nested zip not opened")
            else:
                rep = FileReport(info.filename, None, "skipped")
            result.files.append(rep)
    if conn is not None:
        conn.commit()
    return result
