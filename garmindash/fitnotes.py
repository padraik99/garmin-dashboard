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

# Assumed from the backup's schema (no FitNotes documentation): metric_weight
# is kg, duration_seconds is seconds, Comment.owner_id = training_log._id for
# set comments. distance units are not known yet, so distance_unit stays NULL.
BACKUP_QUERY = """
SELECT t._id, t.date, e.name AS exercise, c.name AS category,
       t.metric_weight, t.reps, t.distance, t.duration_seconds
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


def read_backup(path: str | Path) -> list[dict]:
    src = _open_readonly(path)
    try:
        tables = {r[0].lower() for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = {"training_log", "exercise"} - tables
        if missing:
            raise FitNotesError(f"not a FitNotes backup: no {', '.join(sorted(missing))} table")
        comments: dict[tuple, str] = {}
        if "comment" in tables:
            for r in src.execute("SELECT owner_id, date, comment FROM Comment"):
                if r["comment"]:
                    comments[(r["owner_id"], r["date"])] = r["comment"].strip()
        rows, counter = [], {}
        for r in src.execute(BACKUP_QUERY):
            date = parse_date(str(r["date"]), None)
            key = (date, r["exercise"])
            counter[key] = counter.get(key, 0) + 1
            weight = r["metric_weight"]
            reps = _positive(r["reps"])
            distance = _positive(r["distance"])
            rows.append({
                "fitnotes_id": r["_id"], "date": date, "exercise": r["exercise"],
                "set_number": counter[key], "category": r["category"],
                "weight": float(weight) if weight is not None else None,
                "weight_unit": "kg" if weight is not None else None,
                "reps": int(reps) if reps is not None else None,
                "distance": float(distance) if distance is not None else None,
                "distance_unit": None,
                "time_seconds": int(_positive(r["duration_seconds"]) or 0) or None,
                "comment": comments.get((r["_id"], r["date"])),
            })
        return rows
    finally:
        src.close()


def import_fitnotes_backup(conn: sqlite3.Connection, path: str | Path) -> FitNotesResult:
    res = FitNotesResult()
    rows = read_backup(path)
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
    ids = {r["fitnotes_id"] for r in rows}
    res.missing_from_source = sum(
        1 for (fid,) in conn.execute("SELECT fitnotes_id FROM workouts WHERE fitnotes_id IS NOT NULL")
        if fid not in ids)
    conn.commit()
    return res
