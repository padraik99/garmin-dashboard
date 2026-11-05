"""FitNotes (Android) CSV export -> workouts table.

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


def import_fitnotes(conn: sqlite3.Connection, csv_path: str | Path,
                    date_format: str | None = None) -> FitNotesResult:
    text = Path(csv_path).read_text(encoding="utf-8-sig")
    res = FitNotesResult()
    for row in parse_rows(text, date_format):
        existing = conn.execute(
            f"SELECT {', '.join(VALUE_COLS)} FROM workouts "
            "WHERE date = ? AND exercise = ? AND set_number = ?",
            (row["date"], row["exercise"], row["set_number"])).fetchone()
        if existing is None:
            cols = list(row)
            conn.execute(f"INSERT INTO workouts ({', '.join(cols)}) "
                         f"VALUES ({', '.join('?' * len(cols))})", [row[c] for c in cols])
            res.added += 1
        elif tuple(existing) == tuple(row[c] for c in VALUE_COLS):
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
