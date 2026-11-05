"""Command line: python -m garmindash <command> ..."""
from __future__ import annotations

import argparse
import sqlite3
import sys

from garmindash import db, export, fit, fitnotes

STATUS_MARK = {"used": "USED", "known": "known", "unrecognised": "FLAG",
               "skipped": "skip", "error": "ERROR"}


def _print_fit_result(res: fit.FitImportResult) -> None:
    print(f"activities: {res.added} added, {res.duplicates} already present, "
          f"{res.not_activity} non-activity FIT files skipped, {len(res.errors)} errors")
    for err in res.errors[:20]:
        print(f"  error: {err}")


def cmd_import_export(args) -> int:
    conn = db.connect(args.db)
    result = export.process_export(args.zip, conn)
    print(f"sleep: {result.sleep} nights read, daily: {result.daily} days read (new or updated)")
    _print_fit_result(result.activities)
    breathe = conn.execute("SELECT COUNT(*) FROM breathe_sessions").fetchone()[0]
    print(f"breathe_sessions in db: {breathe}")
    flagged = sum(1 for f in _walk(result.files) if f.status in ("unrecognised", "error"))
    if flagged:
        print(f"{flagged} files flagged; run `python -m garmindash inspect {args.zip}`")
    return 0


def cmd_import_fit(args) -> int:
    conn = db.connect(args.db)
    _print_fit_result(fit.import_fit_folder(conn, args.folder))
    return 0


def cmd_import_fitnotes(args) -> int:
    conn = db.connect(args.db)
    try:
        res = fitnotes.import_fitnotes(conn, args.file, args.date_format)
    except (fitnotes.FitNotesError, sqlite3.DatabaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"workouts: {res.added} added, {res.updated} updated, {res.unchanged} unchanged")
    for r in res.updated_rows[:20]:
        print(f"  updated: {r}")
    if res.missing_from_source:
        print(f"  {res.missing_from_source} sets in the db are no longer in this backup "
              "(deleted in FitNotes?); left in place")
    return 0


def _walk(reports):
    for r in reports:
        yield r
        yield from _walk(r.children)


def cmd_inspect(args) -> int:
    result = export.process_export(args.zip, conn=None)
    for rep in result.files:
        _print_report(rep, 0, args.all)
    print()
    print(f"would import: {result.sleep} sleep nights, {result.daily} daily rows, "
          f"{result.activities.added} activity FIT files")
    flagged = [r for r in _walk(result.files) if r.status in ("unrecognised", "error")]
    print(f"flagged: {len(flagged)}")
    return 0


def _print_report(rep, depth, show_all) -> None:
    pad = "    " * depth
    line = f"{pad}[{STATUS_MARK.get(rep.status, rep.status):5}] {rep.path}"
    if rep.detail:
        line += f"  -- {rep.detail}"
    print(line)
    if rep.children:
        shown = rep.children if show_all else [
            c for c in rep.children if c.status in ("unrecognised", "error")]
        for child in shown:
            _print_report(child, depth + 1, show_all)
        hidden = len(rep.children) - len(shown)
        if hidden:
            print(f"{pad}    ... {hidden} more entries not flagged (use --all to list)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m garmindash",
                                description="Import Garmin and FitNotes data into SQLite.")
    sub = p.add_subparsers(dest="command", required=True)

    def with_db(sp):
        sp.add_argument("--db", default="garmin.db", help="SQLite file (default: garmin.db)")
        return sp

    sp = with_db(sub.add_parser("import-export", help="import a Garmin Connect export ZIP"))
    sp.add_argument("zip")
    sp.set_defaults(func=cmd_import_export)

    sp = with_db(sub.add_parser("import-fit", help="import activity FIT files from a folder"))
    sp.add_argument("folder")
    sp.set_defaults(func=cmd_import_fit)

    sp = with_db(sub.add_parser("import-fitnotes", help="import a FitNotes .fitnotes backup or CSV export"))
    sp.add_argument("file", help=".fitnotes backup or .csv export")
    sp.add_argument("--date-format", default=None,
                    help="CSV only: strptime format if dates are not YYYY-MM-DD, e.g. %%d/%%m/%%Y")
    sp.set_defaults(func=cmd_import_fitnotes)

    sp = sub.add_parser("inspect", help="list an export's files and flag unknown ones")
    sp.add_argument("zip")
    sp.add_argument("--all", action="store_true", help="also list every file inside upload zips")
    sp.set_defaults(func=cmd_inspect)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
