from garmindash.cli import main


def test_inspect_flags_unknown_files(export_zip, capsys):
    assert main(["inspect", str(export_zip)]) == 0
    out = capsys.readouterr().out
    lines = {l.split("]", 1)[1].split("  --")[0].strip(): l for l in out.splitlines() if "]" in l}

    def line(suffix):
        return next(v for k, v in lines.items() if k.endswith(suffix))

    assert line("sleepData.json").startswith("[USED")
    assert "2 of 3 records parsed" in line("12345_sleepData.json")
    assert line("UDSFile_2026-08-01_2026-11-01.json").startswith("[USED")
    assert line("summarizedActivities.json").startswith("[known")
    mystery = line("mysteryMetric.json")
    assert mystery.startswith("[FLAG") and "barValue, foo" in mystery
    broken = line("broken_sleepData.json")
    assert broken.startswith("[FLAG") and "0 of 1 records" in broken and "unexpected" in broken
    mon = line("someone_4444444444.fit")
    assert mon.lstrip().startswith("[FLAG") and "type=monitoring_b" in mon
    assert "4 more entries not flagged" in out  # 3 activity FITs + 1 tcx
    assert "would import: 2 sleep nights, 2 daily rows, 3 activity FIT files" in out


def test_inspect_all_lists_breathe(export_zip, capsys):
    main(["inspect", "--all", str(export_zip)])
    out = capsys.readouterr().out
    assert "meditation/breathing 2026-09-03T12:00:00Z +breathe fields" in out


def test_inspect_writes_no_db(export_zip, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["inspect", str(export_zip)])
    assert not (tmp_path / "garmin.db").exists()


def test_commands_end_to_end(export_zip, watch_folder, tmp_path, capsys):
    dbp = str(tmp_path / "g.db")
    assert main(["import-export", str(export_zip), "--db", dbp]) == 0
    assert main(["import-fit", str(watch_folder), "--db", dbp]) == 0
    out = capsys.readouterr().out
    # run from the watch duplicates the export's run; the 2026-09-05 breathe is new
    assert "activities: 1 added, 1 already present" in out
    csv = tmp_path / "f.csv"
    csv.write_text("Date,Exercise\n2026-09-01,Plank\n")
    assert main(["import-fitnotes", str(csv), "--db", dbp]) == 0
    assert "workouts: 1 added" in capsys.readouterr().out
    bad = tmp_path / "bad.csv"
    bad.write_text("Foo,Bar\n1,2\n")
    assert main(["import-fitnotes", str(bad), "--db", dbp]) == 2
