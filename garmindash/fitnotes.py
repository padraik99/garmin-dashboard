"""FitNotes (Android) CSV export or .fitnotes backup -> workouts table.

Columns are matched by header name, not position. Expected (unconfirmed)
FitNotes header: Date, Exercise, Category, Weight (kgs|lbs), Reps, Distance,
Distance Unit, Time, Comment. Missing columns become NULL.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from garmindash import corrections

COLUMN_ALIASES = {
    "date": ("date", "workout date"),
    "exercise": ("exercise", "exercise name"),
    "category": ("category", "exercise category"),
    "reps": ("reps", "repetitions"),
    "distance_unit": ("distance unit", "distance units"),
    "weight_unit": ("weight unit", "weight units", "unit", "units"),
    "time": ("time", "duration"),
    "comment": ("comment", "comments", "note", "notes"),
}

UNIT_WORDS = {"kg": "kg", "kgs": "kg", "kilograms": "kg", "kilogram": "kg",
              "lb": "lb", "lbs": "lb", "pounds": "lb", "pound": "lb"}


class FitNotesError(ValueError):
    pass


def _norm(header: str) -> str:
    return re.sub(r"\s+", " ", header.strip().lower())


def map_columns(headers: list[str]) -> dict:
    """Return {'date': idx, ..., 'weights': [(idx, unit|None)], 'distance': (idx, unit|None)}."""
    norm = [_norm(h) for h in headers]
    out: dict = {"weights": []}
    for key, aliases in COLUMN_ALIASES.items():
        for i, h in enumerate(norm):
            if h in aliases:
                out[key] = i
                break
    for i, h in enumerate(norm):
        m = re.fullmatch(r"weight\s*(?:\((\w+)\)|\[(\w+)\]|(kgs?|lbs?))?", h)
        if m:
            word = next((g for g in m.groups() if g), None)
            out["weights"].append((i, UNIT_WORDS.get(word) if word else None))
        m = re.fullmatch(r"distance\s*(?:\((\w+)\)|\[(\w+)\])?", h)
        if m and "distance" not in out:
            out["distance"] = (i, next((g for g in m.groups() if g), None))
    missing = [k for k in ("date", "exercise") if k not in out]
    if missing:
        raise FitNotesError(f"CSV has no {' or '.join(missing)} column; headers: {headers}")
    return out


def _num(s: str | None):
    if s is None:
        return None
    s = s.strip().replace(",", ".") if s.count(",") == 1 and "." not in s else s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_time(s: str | None) -> int | None:
    """'1:02:03', '02:03', '75' (seconds) -> seconds."""
    if s is None or not s.strip():
        return None
    s = s.strip()
    if ":" in s:
        parts = s.split(":")
        try:
            secs = 0.0
            for p in parts:
                secs = secs * 60 + float(p)
            return int(round(secs))
        except ValueError:
            return None
    v = _num(s)
    return int(round(v)) if v is not None else None


def parse_date(s: str, fmt: str | None) -> str:
    s = s.strip()
    formats = [fmt] if fmt else ["%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"]
    for f in formats:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise FitNotesError(f"unrecognised date {s!r}; pass --date-format (e.g. %d/%m/%Y)")


def _cell(row, idx):
    if idx is None or idx >= len(row):
        return None
    v = row[idx].strip()
    return v or None


def parse_rows(text: str, date_format: str | None = None) -> list[dict]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        headers = next(reader)
    except StopIteration:
        return []
    cols = map_columns(headers)
    rows, counter = [], {}
    for line_no, raw in enumerate(reader, start=2):
        if not any(c.strip() for c in raw):
            continue
        date_s, exercise = _cell(raw, cols.get("date")), _cell(raw, cols.get("exercise"))
        if not date_s or not exercise:
            continue
        date = parse_date(date_s, date_format)
        weight = unit = None
        for idx, col_unit in cols["weights"]:
            weight = _num(_cell(raw, idx))
            if weight is not None:
                unit = col_unit
                break
        row_unit = _cell(raw, cols.get("weight_unit"))
        if row_unit:
            unit = UNIT_WORDS.get(row_unit.lower(), row_unit)
        if weight is None:
            unit = None
        dist_idx, dist_unit = cols.get("distance", (None, None))
        distance = _num(_cell(raw, dist_idx))
        dist_unit = _cell(raw, cols.get("distance_unit")) or dist_unit
        reps = _num(_cell(raw, cols.get("reps")))
        key = (date, exercise)
        counter[key] = counter.get(key, 0) + 1
        rows.append({
            "date": date, "exercise": exercise, "set_number": counter[key],
            "category": _cell(raw, cols.get("category")),
            "weight": weight, "weight_unit": unit,
            "reps": int(reps) if reps is not None else None,
            "distance": distance, "distance_unit": dist_unit if distance is not None else None,
            "time_seconds": parse_time(_cell(raw, cols.get("time"))),
            "comment": _cell(raw, cols.get("comment")),
        })
    return rows


VALUE_COLS = ("category", "weight", "weight_unit", "reps", "distance",
              "distance_unit", "time_seconds", "comment")


@dataclass
class FitNotesResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    updated_rows: list[str] = field(default_factory=list)
    missing_from_source: int = 0   # backup only: sets in the db that the backup no longer has
    unknown_units: dict = field(default_factory=dict)  # backup only: unmapped unit code -> sets


def is_backup(path: str | Path) -> bool:
    with open(path, "rb") as f:
        return f.read(16) == SQLITE_MAGIC


def import_fitnotes(conn: sqlite3.Connection, path: str | Path,
                    date_format: str | None = None) -> FitNotesResult:
    """Import a FitNotes CSV export or a .fitnotes backup (detected by content)."""
    if is_backup(path):
        return import_fitnotes_backup(conn, path)
    return import_fitnotes_csv(conn, path, date_format)


def import_fitnotes_csv(conn: sqlite3.Connection, csv_path: str | Path,
                        date_format: str | None = None) -> FitNotesResult:
    text = Path(csv_path).read_text(encoding="utf-8-sig")
    res = FitNotesResult()
    for row in parse_rows(text, date_format):
        existing = conn.execute(
            f"SELECT {', '.join(VALUE_COLS)}, fitnotes_id FROM workouts "
            "WHERE date = ? AND exercise = ? AND set_number = ? "
            "ORDER BY fitnotes_id IS NOT NULL LIMIT 1",
            (row["date"], row["exercise"], row["set_number"])).fetchone()
        if existing is not None and existing["fitnotes_id"] is not None:
            res.unchanged += 1  # already imported from a backup, which wins
            continue
        if existing is None:
            cols = list(row)
            conn.execute(f"INSERT INTO workouts ({', '.join(cols)}) "
                         f"VALUES ({', '.join('?' * len(cols))})", [row[c] for c in cols])
            res.added += 1
        elif tuple(existing)[:len(VALUE_COLS)] == tuple(row[c] for c in VALUE_COLS):
            res.unchanged += 1
        else:
            conn.execute(
                f"UPDATE workouts SET {', '.join(c + ' = ?' for c in VALUE_COLS)} "
                "WHERE date = ? AND exercise = ? AND set_number = ?",
                [row[c] for c in VALUE_COLS] + [row["date"], row["exercise"], row["set_number"]])
            res.updated += 1
            res.updated_rows.append(f"{row['date']} {row['exercise']} set {row['set_number']}")
    conn.commit()
    return res


# ---------------------------------------------------------------- .fitnotes backup

SQLITE_MAGIC = b"SQLite format 3\x00"

# Confirmed on a real backup: metric_weight is kg (see below), and a set's
# comments are Comment rows with owner_type_id 1 and owner_id =
# training_log._id (246 of 253 matched; Comment.date is when the comment was
# written, not the workout date). Assumed: duration_seconds is seconds.
# Distance is stored as typed, with training_log.unit naming the unit per
# set; codes confirmed against the app so far are below.
DISTANCE_UNITS = {2: "m", 5: "mi"}

# metric_weight is always kg. FitNotes converts lb -> kg by dividing by
# 2.20462 (10 lb is stored as 4.535929094..., i.e. 10 / 2.20462, not the exact
# 10 / 2.20462262), so multiplying by the same factor restores the typed value.
FITNOTES_LB_PER_KG = 2.20462


def _weight(kg, imperial: bool):
    if kg is None:
        return None, None
    if imperial:
        return round(kg * FITNOTES_LB_PER_KG, 2), "lb"
    return round(float(kg), 3), "kg"


def _is_imperial(src: sqlite3.Connection, tables: set[str]) -> bool:
    """settings.metric = 0 means the app is set to lb (confirmed on a real backup)."""
    if "settings" not in tables:
        return False
    try:
        row = src.execute("SELECT metric FROM settings LIMIT 1").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and row[0] == 0
BACKUP_QUERY = """
SELECT t._id, t.date, e.name AS exercise, c.name AS category,
       t.metric_weight, t.reps, t.distance, t.duration_seconds, t.unit
FROM training_log t
JOIN exercise e ON e._id = t.exercise_id
LEFT JOIN Category c ON c._id = e.category_id
ORDER BY t.date, e.name, t._id
"""


def _open_readonly(path: str | Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _positive(v):
    return v if v is not None and v > 0 else None


def distance_unit(code) -> str:
    """Map training_log.unit to a unit name; unknown codes are kept visibly."""
    return DISTANCE_UNITS.get(code, f"fitnotes_unit_{code}")


def read_backup(path: str | Path) -> list[dict]:
    src = _open_readonly(path)
    try:
        tables = {r[0].lower() for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = {"training_log", "exercise"} - tables
        if missing:
            raise FitNotesError(f"not a FitNotes backup: no {', '.join(sorted(missing))} table")
        comments: dict[int, str] = {}
        if "comment" in tables:
            for r in src.execute("SELECT owner_id, comment FROM Comment "
                                 "WHERE owner_type_id = 1 ORDER BY _id"):
                text = (r["comment"] or "").strip()
                if text:
                    prev = comments.get(r["owner_id"])
                    comments[r["owner_id"]] = f"{prev}\n{text}" if prev else text
        imperial = _is_imperial(src, tables)
        rows, counter = [], {}
        for r in src.execute(BACKUP_QUERY):
            date = parse_date(str(r["date"]), None)
            key = (date, r["exercise"])
            counter[key] = counter.get(key, 0) + 1
            weight, weight_unit = _weight(r["metric_weight"], imperial)
            reps = _positive(r["reps"])
            distance = _positive(r["distance"])
            rows.append({
                "fitnotes_id": r["_id"], "date": date, "exercise": r["exercise"],
                "set_number": counter[key], "category": r["category"],
                "weight": weight, "weight_unit": weight_unit,
                "reps": int(reps) if reps is not None else None,
                "distance": float(distance) if distance is not None else None,
                "distance_unit": distance_unit(r["unit"]) if distance is not None else None,
                "time_seconds": int(_positive(r["duration_seconds"]) or 0) or None,
                "comment": comments.get(r["_id"]),
            })
        return rows
    finally:
        src.close()


def import_fitnotes_backup(conn: sqlite3.Connection, path: str | Path) -> FitNotesResult:
    res = FitNotesResult()
    rows = read_backup(path)
    fixes = corrections.load(conn)
    for row in rows:
        row.update(fixes.get(row["fitnotes_id"], {}))
    cols = ("set_number",) + VALUE_COLS
    for row in rows:
        existing = conn.execute(
            f"SELECT id, {', '.join(cols)} FROM workouts WHERE fitnotes_id = ?",
            (row["fitnotes_id"],)).fetchone()
        if existing is None:
            # A CSV import of the same set: claim it rather than duplicate it.
            existing = conn.execute(
                f"SELECT id, {', '.join(cols)} FROM workouts WHERE fitnotes_id IS NULL "
                "AND date = ? AND exercise = ? AND set_number = ?",
                (row["date"], row["exercise"], row["set_number"])).fetchone()
        if existing is None:
            names = list(row)
            conn.execute(f"INSERT INTO workouts ({', '.join(names)}) "
                         f"VALUES ({', '.join('?' * len(names))})", [row[c] for c in names])
            res.added += 1
            continue
        new_vals = tuple(row[c] for c in cols)
        conn.execute(
            f"UPDATE workouts SET fitnotes_id = ?, {', '.join(c + ' = ?' for c in cols)} "
            "WHERE id = ?", (row["fitnotes_id"],) + new_vals + (existing["id"],))
        if tuple(existing)[1:] == new_vals:
            res.unchanged += 1
        else:
            res.updated += 1
            res.updated_rows.append(f"{row['date']} {row['exercise']} set {row['set_number']}")
    for row in rows:
        unit = row["distance_unit"]
        if unit and unit.startswith("fitnotes_unit_"):
            res.unknown_units[unit] = res.unknown_units.get(unit, 0) + 1
    ids = {r["fitnotes_id"] for r in rows}
    res.missing_from_source = sum(
        1 for (fid,) in conn.execute("SELECT fitnotes_id FROM workouts WHERE fitnotes_id IS NOT NULL")
        if fid not in ids)
    conn.commit()
    return res
