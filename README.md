# garmin-dashboard

Personal dashboard for a Garmin Venu 2: sleep, resting HR, stress, Body
Battery, steps, activities, sessions from a custom Breathe Connect IQ app, and
gym workouts from FitNotes. This repo is currently the **import layer only**:
it loads everything into one SQLite file (`garmin.db`). Charts come later.

No Garmin Connect API is used. Data comes from:

1. one Garmin Connect account export ("Export your data") for the full history,
2. activity FIT files copied off the watch over USB for ongoing top-ups,
3. FitNotes CSV exports for gym sessions.

## Setup

Python 3.11 or newer.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

(`source .venv/bin/activate` on macOS/Linux.)

```bash
pip install -e ".[dev]"
```

```bash
python -m pytest
```

Keep personal files in `data/` (git-ignored, as are `*.db`, `*.zip`, `*.fit`
and `*.csv` outside `tests/`).

## Commands

Every import command takes `--db PATH` (default `garmin.db` in the current
folder) and creates the tables on first use. All imports are safe to re-run.

### `inspect <zip>` — run this first on a new export

```bash
python -m garmindash inspect data/garmin_export.zip
```

Dry run: reads the export without touching any database and prints one line
per file:

| Mark    | Meaning |
|---------|---------|
| `USED`  | importer read it (with how many records parsed) |
| `known` | a Garmin file the importer deliberately ignores for now |
| `FLAG`  | unrecognised JSON (top-level keys shown), a FIT file that isn't an activity (file type and message counts shown), or a recognised file name whose records didn't parse |
| `ERROR` | invalid JSON / corrupt FIT / bad zip |
| `skip`  | anything else |

Inside `UploadedFiles_*.zip` only flagged entries are listed; add `--all` to
list every FIT file with its sport, start time and whether it carries Breathe
fields. A `FLAG` line is the signal to adjust the matching rules in
`garmindash/export.py`.

### `import-export <zip>`

```bash
python -m garmindash import-export data/garmin_export.zip
```

Files are matched by name anywhere in the archive (case-insensitive):

| Pattern | Goes to |
|---------|---------|
| `*sleepData*.json` (usually `DI_CONNECT/DI-Connect-Wellness/`) | `sleep` |
| `UDSFile_*.json` (usually `DI_CONNECT/DI-Connect-Aggregator/`) | `daily` |
| `UploadedFiles_*.zip` (usually `DI_CONNECT/DI-Connect-Uploaded-Files/`) — every `.fit` inside | `activities`, `breathe_sessions` |
| any loose `*.fit` | `activities`, `breathe_sessions` |

Sleep and daily rows are upserted by date; a later or overlapping file fills
in or overwrites values but never replaces a value with NULL. Activities are
deduplicated (see below). Each field is looked up under several candidate key
names because Garmin's JSON has changed over the years.

### `import-fit <folder>`

```bash
python -m garmindash import-fit E:\GARMIN\Activity
```

Imports every `*.fit` under the folder, recursively. Pointing it at the whole
`GARMIN` folder is fine: non-activity FIT files (monitoring, settings, sleep)
are counted and skipped, corrupt files are reported and skipped.

Deduplication: an activity is skipped if the same file (SHA-256) or an
activity with the same start time and sport is already in the database. So
the watch copy and the export copy of one run produce one row, in either
import order; if the watch copy came first, the export fills in its Garmin
Connect activity ID.

Note: USB top-ups only cover **activities**. Sleep, stress, Body Battery and
daily summaries live in the watch's monitoring/sleep FIT files, whose format is
largely undocumented; those tables only grow with a fresh account export.

### `import-fitnotes <csv>`

```bash
python -m garmindash import-fitnotes data/FitNotes_Export.csv
```

Columns are matched by header name, so order and missing columns don't
matter. Only `Date` and `Exercise` are required. Recognised headers
(case-insensitive): `Date`, `Exercise`, `Category`, `Weight (kgs)` /
`Weight (lbs)` / `Weight` + `Weight Unit`, `Reps`, `Distance`,
`Distance Unit`, `Time` (`h:mm:ss`, `mm:ss` or seconds), `Comment`/`Notes`.
Dates must be `YYYY-MM-DD` unless you pass e.g. `--date-format %d/%m/%Y`.

Re-importing a newer full export adds only new sets. Each set is keyed by
(date, exercise, set number), where set number is its position among that
day's rows for that exercise. A set edited in FitNotes since the last import
is updated and listed; a set deleted in FitNotes stays in the database.

## Tables

All timestamps ending `_utc` are ISO 8601 UTC with a `Z` suffix
(`2026-09-01T11:00:00Z`). Dates are `YYYY-MM-DD`.

### `sleep` (key: `night_date`)

| Column | Notes |
|--------|-------|
| `night_date` | Garmin's `calendarDate`: the date you woke up |
| `start_utc`, `end_utc` | sleep window |
| `total_sleep_seconds` | deep + light + REM (awake excluded), unless Garmin gives a total |
| `deep_seconds`, `light_seconds`, `rem_seconds`, `awake_seconds` | stage totals |
| `source_file` | export file it came from |

### `daily` (key: `date`)

| Column | Notes |
|--------|-------|
| `resting_hr` | bpm |
| `avg_stress`, `max_stress` | whole-day ("TOTAL") values, 0–100; Garmin's -1/-2 "no data" stored as NULL |
| `body_battery_high`, `body_battery_low` | day's highest / lowest |
| `body_battery_charged`, `body_battery_drained` | day's totals |
| `steps` | |
| `source_file` | |

### `activities` (key: `id`; unique: `(start_utc, sport)` and `file_sha256`)

| Column | Notes |
|--------|-------|
| `id` | local row id |
| `garmin_activity_id` | Connect activity ID from the export file name, if known |
| `start_utc` | session start |
| `start_local` | watch wall-clock start (no offset), from the FIT `activity.local_timestamp` |
| `sport`, `sub_sport` | FIT enum names, e.g. `running`/`generic`, `meditation`/`breathing` |
| `duration_seconds` | timer time (excludes pauses) |
| `elapsed_seconds` | wall time including pauses |
| `distance_m` | metres |
| `avg_hr`, `max_hr`, `calories` | |
| `source_file`, `file_sha256` | where it came from |

Only the first session of a multi-sport activity is stored.

### `breathe_sessions` (key: `activity_id` → `activities.id`)

Created for any activity whose FIT session (or, failing that, lap) message
carries Connect IQ developer fields with these names. Names are matched
ignoring case and separators, so `HR Pre` also maps to `hr_pre`.

| Column | Type | |
|--------|------|-|
| `start_utc` | text | same as the activity |
| `pattern` | text | breathing pattern |
| `minutes` | int | |
| `breaths` | int | |
| `hr_pre`, `hr_post` | real | heart rate before / after |
| `rmssd_pre`, `rmssd_post` | real | HRV (RMSSD, ms) before / after |
| `extra_json` | text | any other developer fields, verbatim |

The Venu 2 has no overnight HRV Status, so these RMSSD readings are the HRV
series for the dashboard.

### `workouts` (key: `id`; unique: `(date, exercise, set_number)`)

| Column | Notes |
|--------|-------|
| `date`, `exercise`, `set_number`, `category` | |
| `weight`, `weight_unit` | as logged; unit `kg` or `lb` (no conversion) |
| `reps` | |
| `distance`, `distance_unit` | |
| `time_seconds` | |
| `comment` | |

## Tests

`python -m pytest`. Fixtures are synthetic and generated at test time:
`tests/fitwriter.py` is a small FIT encoder (written from the FIT protocol
spec, including developer fields), and `tests/conftest.py` builds a fake
export ZIP and fake FitNotes CSVs. No real personal data is in this repo.
