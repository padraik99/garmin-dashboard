"""Manual corrections to FitNotes sets that can't be fixed in the app.

A correction is stored per set (by FitNotes' own set id) and field, and is
re-applied on every later backup import, so a newer backup never undoes it.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

FIELDS = {"category": str, "weight": float, "weight_unit": str, "reps": int,
          "distance": float, "distance_unit": str, "time_seconds": int, "comment": str}


class CorrectionError(ValueError):
    pass


def parse_day(s: str) -> str:
    """YYYY-MM-DD, or US-style M-D-YYYY / M/D/YYYY."""
    for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise CorrectionError(f"unrecognised date {s!r}; use YYYY-MM-DD or M-D-YYYY")


def parse_assignment(text: str) -> tuple[str, object]:
    if "=" not in text:
        raise CorrectionError(f"expected field=value, got {text!r}")
    name, raw = (p.strip() for p in text.split("=", 1))
    if name not in FIELDS:
        raise CorrectionError(f"can't correct {name!r}; fields: {', '.join(FIELDS)}")
    if raw == "":
        return name, None
    try:
        return name, FIELDS[name](raw)
    except ValueError:
        raise CorrectionError(f"{name} must be a {FIELDS[name].__name__}, got {raw!r}") from None


def sets_for(conn: sqlite3.Connection, date: str, exercise: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM workouts WHERE date = ? AND lower(exercise) = lower(?) "
        "ORDER BY set_number, id", (date, exercise)).fetchall()


def add(conn: sqlite3.Connection, date: str, exercise: str, set_number: int,
        assignments: dict, note: str | None = None) -> tuple[dict, dict]:
    """Store and apply corrections; returns (before, after) for the changed fields."""
    rows = [r for r in sets_for(conn, date, exercise) if r["set_number"] == set_number]
    if not rows:
        have = sets_for(conn, date, exercise)
        hint = (f"sets on that day: {', '.join(str(r['set_number']) for r in have)}" if have
                else "no sets for that exercise on that day (check the spelling)")
        raise CorrectionError(f"no set {set_number} of {exercise!r} on {date}; {hint}")
    if len(rows) > 1:
        raise CorrectionError("more than one set matches; this shouldn't happen, please report it")
    row = rows[0]
    if row["fitnotes_id"] is None:
        raise CorrectionError("that set came from a CSV import; corrections need a "
                              ".fitnotes backup import so they can survive re-imports")
    before = {k: row[k] for k in assignments}
    for name, value in assignments.items():
        conn.execute("INSERT OR REPLACE INTO workout_corrections "
                     "(fitnotes_id, field, value, note) VALUES (?, ?, ?, ?)",
                     (row["fitnotes_id"], name, value, note))
        conn.execute(f"UPDATE workouts SET {name} = ? WHERE id = ?", (value, row["id"]))
    conn.commit()
    return before, dict(assignments)


def load(conn: sqlite3.Connection) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for fid, name, value in conn.execute(
            "SELECT fitnotes_id, field, value FROM workout_corrections"):
        if name in FIELDS:
            out.setdefault(fid, {})[name] = value
    return out
