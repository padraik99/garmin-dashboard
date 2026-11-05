"""SQLite schema and connection helper."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sleep (
    night_date          TEXT PRIMARY KEY,   -- Garmin calendarDate (the morning you woke up)
    start_utc           TEXT,               -- ISO 8601, UTC, 'Z' suffix
    end_utc             TEXT,
    total_sleep_seconds INTEGER,            -- deep + light + REM (awake excluded)
    deep_seconds        INTEGER,
    light_seconds       INTEGER,
    rem_seconds         INTEGER,
    awake_seconds       INTEGER,
    source_file         TEXT
);

CREATE TABLE IF NOT EXISTS daily (
    date                  TEXT PRIMARY KEY,
    resting_hr            INTEGER,
    avg_stress            INTEGER,
    max_stress            INTEGER,
    body_battery_high     INTEGER,
    body_battery_low      INTEGER,
    body_battery_charged  INTEGER,
    body_battery_drained  INTEGER,
    steps                 INTEGER,
    source_file           TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    id                INTEGER PRIMARY KEY,
    garmin_activity_id INTEGER,            -- from export file name, when known
    start_utc         TEXT NOT NULL,
    start_local       TEXT,                -- wall-clock time on the watch, no offset
    sport             TEXT NOT NULL,
    sub_sport         TEXT,
    duration_seconds  REAL,                -- total_timer_time (moving/recording time)
    elapsed_seconds   REAL,                -- total_elapsed_time
    distance_m        REAL,
    avg_hr            INTEGER,
    max_hr            INTEGER,
    calories          INTEGER,
    source_file       TEXT,
    file_sha256       TEXT UNIQUE,
    UNIQUE (start_utc, sport)
);

CREATE TABLE IF NOT EXISTS breathe_sessions (
    activity_id  INTEGER PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
    start_utc    TEXT NOT NULL,
    pattern      TEXT,
    minutes      INTEGER,
    breaths      INTEGER,
    hr_pre       REAL,
    hr_post      REAL,
    rmssd_pre    REAL,
    rmssd_post   REAL,
    extra_json   TEXT                  -- any other developer fields, verbatim
);

CREATE TABLE IF NOT EXISTS workouts (
    id            INTEGER PRIMARY KEY,
    fitnotes_id   INTEGER UNIQUE,      -- training_log._id from a .fitnotes backup; NULL for CSV rows
    date          TEXT NOT NULL,
    exercise      TEXT NOT NULL,
    set_number    INTEGER NOT NULL,    -- 1-based position within (date, exercise)
    category      TEXT,
    weight        REAL,
    weight_unit   TEXT,                -- 'kg' or 'lb'
    reps          INTEGER,
    distance      REAL,
    distance_unit TEXT,
    time_seconds  INTEGER,
    comment       TEXT
);
-- CSV rows have no stable id, so they are keyed by position instead.
CREATE UNIQUE INDEX IF NOT EXISTS workouts_csv_key
    ON workouts(date, exercise, set_number) WHERE fitnotes_id IS NULL;

-- Manual fixes (python -m garmindash correct-workout), re-applied on every backup import.
CREATE TABLE IF NOT EXISTS workout_corrections (
    fitnotes_id INTEGER NOT NULL,
    field       TEXT NOT NULL,
    value,
    note        TEXT,
    PRIMARY KEY (fitnotes_id, field)
);

CREATE INDEX IF NOT EXISTS activities_start ON activities(start_utc);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
