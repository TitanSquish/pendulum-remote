import asyncio
import json

from fastapi.testclient import TestClient

import bridge as bridge_mod
from bridge import Bridge, make_app


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class FakeBridge(Bridge):
    def __init__(self, runs_dir):
        super().__init__(runs_dir)
        self.lines, self.pushed = [], []

    def send_line(self, line):
        self.lines.append(line)

    def push(self, msg):
        self.pushed.append(msg)

    def start(self):
        pass


def feed_run(b, n=25, end="run end stop"):
    b.handle_line("run start SwingBal BAL_POLE=5")
    for i in range(n):
        b.handle_line(f"D {2 * i} 0.01 3.1416 0.1 nan")
    b.handle_line(end)


def test_select_and_start_need_control(tmp_path):
    b, ws = FakeBridge(tmp_path), FakeWS()

    async def go():
        b.clients.add(ws)
        await b.command(ws, {"cmd": "select", "name": "CartPID"})
        assert b.lines == [] and ws.sent[-1]["text"] == "take control first"
        await b.command(ws, {"cmd": "claim"})
        await b.command(ws, {"cmd": "select", "name": "CartPID"})
        await b.command(ws, {"cmd": "select", "name": "x; stop"})      # rejected
        await b.command(ws, {"cmd": "start"})

    asyncio.run(go())
    assert b.lines == ["exp select CartPID", "run"]


def test_heartbeat_loss_during_run_sends_stop(tmp_path):
    b, ws = FakeBridge(tmp_path), FakeWS()

    async def go():
        b.clients.add(ws)
        await b.command(ws, {"cmd": "claim"})
        b.state = "BALANCE"
        await b.check_heartbeat(b.last_beat + 1.0)       # still within the timeout
        assert "stop" not in b.lines
        await b.check_heartbeat(b.last_beat + 10.0)

    asyncio.run(go())
    assert b.lines.count("stop") == 1 and b.controller is None


def test_run_lines_are_recorded_saved_and_decimated(tmp_path):
    b = FakeBridge(tmp_path)
    feed_run(b)
    kinds = [m["kind"] for m in b.pushed]
    assert kinds.count("telemetry") == 2                 # every 10th of 25 samples
    assert not any(m["kind"] == "log" and m["text"].startswith("D ") for m in b.pushed)
    tel = next(m for m in b.pushed if m["kind"] == "telemetry")
    assert tel["ref_mm"] is None and abs(abs(tel["th_deg"]) - 180) < 0.01 and tel["x_mm"] == 10.0
    end = next(m for m in b.pushed if m["kind"] == "run_end")
    assert end["samples"] == 25 and end["reason"] == "stop"
    assert (tmp_path / end["files"]["csv"]).exists() and (tmp_path / end["files"]["mat"]).exists()
    assert "status" in b.lines                           # asks for the new state after run end


def test_status_and_exp_lines(tmp_path):
    b = FakeBridge(tmp_path)
    b.handle_line("exp SwingBal | Swing-up & balance")
    b.handle_line("exp StepTest | Speed loop step test")
    b.handle_line("exp end")
    assert b.experiments == [{"name": "SwingBal", "title": "Swing-up & balance"},
                             {"name": "StepTest", "title": "Speed loop step test"}]
    b.handle_line("status BALANCE SwingBal")
    assert b.state == "BALANCE" and b.exp == "SwingBal"
    b.handle_line("ok exp StepTest")
    assert b.exp == "StepTest" and "params" in b.lines
    assert {"kind": "exp", "name": "StepTest"} in b.pushed


def test_serial_lost_saves_partial_run(tmp_path):
    b = FakeBridge(tmp_path)
    b.handle_line("run start SwingBal")
    b.handle_line("D 0 0 3.14 0 nan")
    b.serial_lost()
    end = next(m for m in b.pushed if m["kind"] == "run_end")
    assert end["reason"] == "disconnected" and end["samples"] == 1
    assert b.state == "OFFLINE"


def test_runs_endpoints(tmp_path, monkeypatch):
    b = FakeBridge(tmp_path)
    feed_run(b)
    client = TestClient(make_app(b))                     # no "with": lifespan (serial) not started
    r = client.get("/runs")
    assert r.status_code == 200 and r.json()[0]["reason"] == "stop"
    csv_name = r.json()[0]["files"]["csv"]
    assert client.get(f"/runs/{csv_name}").status_code == 200
    assert client.get("/runs/..%2Fbridge.py").status_code == 404
    assert client.get("/runs/missing.csv").status_code == 404
    assert client.get("/runs/" + csv_name.replace(".csv", ".json")).status_code == 404
    monkeypatch.setattr(bridge_mod, "TOKEN", "s3cret")
    assert client.get("/runs").status_code == 401
    assert client.get("/runs?token=s3cret").status_code == 200
    assert client.get(f"/runs/{csv_name}").status_code == 401
    assert client.get(f"/runs/{csv_name}?token=s3cret").status_code == 200
