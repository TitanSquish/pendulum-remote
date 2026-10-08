import json
import math

import pytest

from runs import Recorder, Run, parse_d_line, parse_run_start, save_run


def test_parse_d_line():
    row = parse_d_line("D 1234 0.0123 3.1416 0.250 nan")
    assert row[:4] == (1.234, 0.0123, 3.1416, 0.25)
    assert math.isnan(row[4])
    assert parse_d_line("D 10 0 0 0 nan 0.5")[5] == 0.5          # optional r column


@pytest.mark.parametrize("bad", [
    "D 12 0.1",                       # too short
    "D x 0 0 0 nan",                  # not a number
    "D 12 nan 0 0 nan",               # x must be a number
    "D 1 2 3 4 5 6 7 8",              # too many fields
    "\ufffd\ufffdight 0 start 0",     # USB garbage
])
def test_parse_d_line_rejects(bad):
    assert parse_d_line(bad) is None


def test_parse_run_start():
    assert parse_run_start("run start CartPID P=27.8 I=50 junk D=nan") == ("CartPID", {"P": 27.8, "I": 50.0})
    assert parse_run_start("run end stop") is None


def test_recorder_full_run():
    rec = Recorder()
    ev = rec.on_line("run start SwingBal BAL_POLE=5", now=1000.0)
    assert ev[0][0] == "start" and ev[0][1].params == {"BAL_POLE": 5.0}
    assert rec.on_line("D 0 0 3.1416 0 nan")[0][0] == "sample"
    rec.on_line("D 2 0.001 3.14 0.1 nan")
    assert rec.on_line("result friction_pos_V 0.21") == [("result", ("friction_pos_V", 0.21))]
    ev = rec.on_line("run end stop")
    kind, run = ev[0]
    assert kind == "end" and run.reason == "stop" and len(run.rows) == 2
    assert run.results == {"friction_pos_V": 0.21} and run.started == 1000.0
    assert rec.run is None


def test_garbled_d_lines_are_counted_not_recorded():
    rec = Recorder()
    rec.on_line("run start SwingBal")
    rec.on_line("D 4 0.1 \ufffd\ufffd")
    rec.on_line("D 6 0.1 3.1 0 nan")
    run = rec.on_line("run end done")[0][1]
    assert run.dropped == 1 and len(run.rows) == 1


def test_lines_outside_a_run_are_ignored():
    rec = Recorder()
    assert rec.on_line("D 0 0 3.14 0 nan") == []
    assert rec.on_line("run end done") == []
    assert rec.on_line("result k 1") == []


def test_restart_without_end_closes_previous_as_interrupted():
    rec = Recorder()
    rec.on_line("run start SwingBal")
    rec.on_line("D 0 0 3.14 0 nan")
    ev = rec.on_line("run start StepTest")
    assert [k for k, _ in ev] == ["end", "start"]
    assert ev[0][1].reason == "interrupted" and len(ev[0][1].rows) == 1
    assert rec.run.name == "StepTest"


def test_abort_keeps_partial_run():
    rec = Recorder()
    rec.on_line("run start SwingBal")
    rec.on_line("D 0 0 3.14 0 nan")
    run = rec.abort("disconnected")
    assert run.reason == "disconnected" and len(run.rows) == 1
    assert rec.abort("disconnected") is None


def _sample_run(name="CartPID"):
    return Run(name, {"P": 27.8}, started=1791400000.0,
               rows=[(0.0, 0.0, 3.14, 0.0, math.nan), (0.002, 0.001, 3.13, 0.25, 0.05)],
               results={"k": 1.0}, reason="done")


def test_save_run_writes_csv_mat_and_summary(tmp_path):
    summary = save_run(_sample_run("Cart/PID"), tmp_path)
    csv_path = tmp_path / summary["files"]["csv"]
    assert summary["files"]["csv"].endswith("_Cart_PID.csv")
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert "# experiment Cart/PID" in lines and "# end done" in lines and "# param P 27.8" in lines
    assert "t,x,theta,u,ref" in lines
    assert lines[-1] == "0.002,0.001,3.13,0.25,0.05"
    assert lines[-2] == "0.0,0.0,3.14,0.0,nan"
    assert (tmp_path / summary["files"]["mat"]).exists()
    on_disk = json.loads((tmp_path / (csv_path.stem + ".json")).read_text(encoding="utf-8"))
    assert on_disk == summary
    assert summary["samples"] == 2 and summary["duration"] == 0.002 and summary["reason"] == "done"


def test_save_run_never_overwrites(tmp_path):
    a = save_run(_sample_run(), tmp_path)
    b = save_run(_sample_run(), tmp_path)          # same name, same second
    assert a["files"]["csv"] != b["files"]["csv"]
    assert len(list(tmp_path.glob("*.csv"))) == 2
