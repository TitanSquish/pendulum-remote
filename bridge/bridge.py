"""Pendulum remote bridge: ESP32 (USB serial) <-> browsers (WebSocket).

    python bridge/bridge.py                 # auto-detect the rig's serial port
    python bridge/bridge.py --port COM3     # or name it
    python bridge/bridge.py --mock          # simulated rig, no hardware needed

Then open http://localhost:8000

- Talks to the single-board firmware (cart_single v11.1+) at 921600 baud:
  experiments, settings, run control and telemetry.
- Records every run (the firmware's "run start" / "D" / "run end" lines) and
  saves it to runs/ as CSV + MATLAB .mat, with a .json summary the console
  lists. Downloads: GET /runs and /runs/<file>.
- One controller at a time ("take control"). Anyone connected can press STOP.
- If the controlling browser disconnects or stops sending heartbeats while the
  rig is running, the bridge sends "stop".
- Optional password: set BRIDGE_TOKEN=... and open http://host:8000/?token=...

Close the Arduino serial monitor first: only one program can open the port.
"""

import argparse
import asyncio
import json
import math
import os
import random
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import serial
import serial.tools.list_ports
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from runs import Recorder, run_summary, save_run

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
TOKEN = os.environ.get("BRIDGE_TOKEN", "")
BAUD = 921600
HEARTBEAT_TIMEOUT_S = 3.0
STATUS_POLL_S = 2.0
DECIMATE = 10                    # D lines arrive at 500 Hz; browsers get every 10th (50 Hz)
RUN_FILE = re.compile(r"[A-Za-z0-9_-]+\.(csv|mat)")
EXP_NAME = re.compile(r"[A-Za-z0-9_]{1,32}")

# ----------------------------------------------------------------------------
# Parsing the firmware's human-readable serial lines
# ----------------------------------------------------------------------------
NUM = r"(-?[\d.]+)"
TELEMETRY = {
    "SWING": re.compile(rf"^SWING th\s+{NUM} deg\s+w\s+{NUM}\s+x\s+{NUM} mm\s+v\s+{NUM}\s+vr\s+{NUM}\s+E\s+{NUM}\s+Eend\s+{NUM}\s+u\s+{NUM}"),
    "BAL":   re.compile(rf"^BAL\s+th\s+{NUM} deg\s+w\s+{NUM}\s+x\s+{NUM} mm\s+v\s+{NUM}\s+vr\s+{NUM}\s+trim\s+{NUM} deg\s+u\s+{NUM}"),
}
FIELDS = {
    "SWING": ["th_deg", "w", "x_mm", "v", "vr", "E", "Eend", "u"],
    "BAL":   ["th_deg", "w", "x_mm", "v", "vr", "trim_deg", "u"],
}
HANG = re.compile(rf"^HANG\s+pend\s+{NUM}\s+swing\s+{NUM}")
PARAM = re.compile(r"^param (\S+) (\S+) (\S+) (\S+) (\S+) (\S+) \| (.*)$")
EVENT_PREFIXES = ("SOFT LIMIT", "OVERSPEED", "LIMIT HIT", "STOP", "Stopped at", "Homing complete",
                  "ERROR", "Auto re-home", "Balance gains", "Hanging reference", "Balance holding",
                  "START from", "Homing: driving left", "=== ESP32", "Ready.", "WARNING",
                  "Step test done")
STATUS_REFRESH = ("STOP triggered", "Stopped at", "ERROR", "LIMIT HIT", "run end", "Ready.")
STATE_NAMES = {"STEP TEST": "STEP"}


def wrap_deg(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


class Bridge:
    """Shared state + the browser side. Subclasses provide send_line()."""

    def __init__(self, runs_dir: Path = RUNS_DIR):
        self.clients: set[WebSocket] = set()
        self.controller: WebSocket | None = None
        self.last_beat = 0.0
        self.state = "OFFLINE"
        self.params: list[dict] = []
        self._param_buf: list[dict] = []
        self.experiments: list[dict] = []
        self._exp_buf: list[dict] = []
        self.exp = ""
        self.runs_dir = runs_dir
        self.recorder = Recorder()
        self._n_samples = 0
        self.loop: asyncio.AbstractEventLoop | None = None

    # --- to browsers ---
    async def broadcast(self, msg: dict):
        data = json.dumps(msg)
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.drop(ws)

    def push(self, msg: dict):
        """Thread-safe broadcast (from the serial reader thread)."""
        if self.loop:
            self.loop.call_soon_threadsafe(asyncio.ensure_future, self.broadcast(msg))

    def snapshot(self, ws: WebSocket) -> dict:
        return {"kind": "hello", "state": self.state, "params": self.params,
                "control": self._role(ws), "clients": len(self.clients),
                "experiments": self.experiments, "exp": self.exp}

    def _role(self, ws: WebSocket) -> str:
        if self.controller is ws:
            return "you"
        return "other" if self.controller else "free"

    async def announce_control(self):
        for ws in list(self.clients):
            try:
                await ws.send_text(json.dumps({"kind": "control", "control": self._role(ws),
                                               "clients": len(self.clients)}))
            except Exception:
                pass

    # --- from the board ---
    def handle_line(self, line: str):
        line = line.strip()
        if not line:
            return
        if line.startswith(("D ", "run start", "run end", "result ")):
            for kind, payload in self.recorder.on_line(line):
                self._on_run_event(kind, payload)
            if line.startswith("D "):
                return                          # 500 Hz: recorded, not logged as text
        for kind, rx in TELEMETRY.items():
            m = rx.match(line)
            if m:
                vals = dict(zip(FIELDS[kind], (float(g) for g in m.groups())))
                self.push({"kind": "telemetry", "mode": kind, "t": time.time(), **vals})
                self._set_state("BALANCE" if kind == "BAL" else "SWING")
                return
        m = HANG.match(line)
        if m:
            self.push({"kind": "telemetry", "mode": "HANG", "t": time.time(),
                       "pend": float(m.group(1)), "swing": float(m.group(2))})
            self._set_state("HANG")       # the firmware enters HANG without a ">>" line
            return
        if line.startswith("Homing: driving left"):
            self._set_state("HOMING")     # no ">>" line when homing starts either
        if line.startswith(">> "):
            self._set_state(line[3:].strip())
        elif line.startswith("status "):
            parts = line[7:].split()
            if parts:
                self._set_state(parts[0])
                if len(parts) > 1:
                    self._set_exp(parts[1])
        elif line.startswith("exp ") and " | " in line:
            name, _, title = line[4:].partition(" | ")
            self._exp_buf.append({"name": name.strip(), "title": title.strip()})
        elif line == "exp end":
            self.experiments, self._exp_buf = self._exp_buf, []
            self.push({"kind": "experiments", "experiments": self.experiments})
        elif line.startswith("param "):
            m = PARAM.match(line)
            if m:
                name, val, lo, hi, de, unit, desc = m.groups()
                self._param_buf.append({"name": name, "value": float(val), "min": float(lo),
                                        "max": float(hi), "default": float(de),
                                        "unit": "" if unit == "-" else unit, "desc": desc})
        elif line == "params end":
            self.params, self._param_buf = self._param_buf, []
            self.push({"kind": "params", "params": self.params})
        elif line.startswith("ok ") or line.startswith("err "):
            ok = line.startswith("ok ")
            self.push({"kind": "reply", "ok": ok, "text": line[3 if ok else 4:]})
            words = line.split()
            if ok and len(words) > 2 and words[1] == "exp":
                self._set_exp(words[2])
            if ok and len(words) > 1 and words[1] not in ("stop", "home"):
                self.send_line("params")     # a setting or the experiment changed
        else:
            if line.startswith(EVENT_PREFIXES):
                self.push({"kind": "event", "text": line, "t": time.time()})
            if line.startswith(STATUS_REFRESH) or "aborted" in line:
                self.send_line("status")
        self.push({"kind": "log", "text": line})

    def _on_run_event(self, kind: str, payload):
        if kind == "start":
            self._n_samples = 0
            self.push({"kind": "run_start", "name": payload.name, "params": payload.params,
                       "t": payload.started})
        elif kind == "sample":
            self._n_samples += 1
            if self._n_samples % DECIMATE == 0:
                t, x, th, u, ref = payload[:5]
                self.push({"kind": "telemetry", "mode": "D", "t": time.time(), "run_t": t,
                           "x_mm": x * 1000.0, "th_deg": wrap_deg(math.degrees(th)), "u": u,
                           "ref_mm": None if math.isnan(ref) else ref * 1000.0})
        elif kind == "result":
            key, value = payload
            self.push({"kind": "event", "text": f"Result: {key} = {value:g}", "t": time.time()})
        elif kind == "end":
            try:
                summary = save_run(payload, self.runs_dir)
            except OSError as e:
                print(f"[bridge] could not save the run: {e}")
                summary = run_summary(payload, {})
            self.push({"kind": "run_end", **summary})

    def serial_lost(self):
        """The board is gone: keep whatever the open run recorded."""
        run = self.recorder.abort("disconnected")
        if run:
            self._on_run_event("end", run)
        self._set_state("OFFLINE")

    def _set_state(self, s: str):
        s = STATE_NAMES.get(s, s)
        if s != self.state:
            self.state = s
            self.push({"kind": "state", "state": s})

    def _set_exp(self, name: str):
        if name != self.exp:
            self.exp = name
            self.push({"kind": "exp", "name": name})

    # --- commands from a browser ---
    async def command(self, ws: WebSocket, msg: dict):
        cmd = msg.get("cmd")
        if cmd == "ping":
            if ws is self.controller:
                self.last_beat = time.time()
            return
        if cmd == "stop":                       # anyone may stop
            self.send_line("stop")
            return
        if cmd == "claim":
            if self.controller is None:
                self.controller, self.last_beat = ws, time.time()
            await self.announce_control()
            return
        if cmd == "release":
            if ws is self.controller:
                await self.release(stop_if_running=True)
            return
        if cmd == "params":
            self.send_line("params")
            return
        if ws is not self.controller:
            await ws.send_text(json.dumps({"kind": "reply", "ok": False,
                                           "text": "take control first"}))
            return
        if cmd == "start":
            self.send_line("run")
        elif cmd == "select":
            name = str(msg.get("name", ""))
            if not EXP_NAME.fullmatch(name):
                await ws.send_text(json.dumps({"kind": "reply", "ok": False, "text": "bad experiment"}))
                return
            self.send_line(f"exp select {name}")
        elif cmd == "defaults":
            self.send_line("defaults")
        elif cmd == "set":
            name, value = str(msg.get("name", "")), msg.get("value")
            if not re.fullmatch(r"[A-Z_]+", name) or not isinstance(value, (int, float)) \
               or not math.isfinite(value):
                await ws.send_text(json.dumps({"kind": "reply", "ok": False, "text": "bad set"}))
                return
            self.send_line(f"set {name} {value:g}")   # the board checks the limits

    async def release(self, stop_if_running: bool):
        if stop_if_running and self.state not in ("IDLE", "OFFLINE"):
            self.send_line("stop")
        self.controller = None
        await self.announce_control()

    async def drop(self, ws: WebSocket):
        self.clients.discard(ws)
        if ws is self.controller:
            await self.release(stop_if_running=True)
        else:
            await self.announce_control()

    async def check_heartbeat(self, now: float):
        """Stop the rig if the controller stopped sending heartbeats."""
        if self.controller and now - self.last_beat > HEARTBEAT_TIMEOUT_S:
            self.push({"kind": "event", "text": "Controller heartbeat lost — stopping.",
                       "t": time.time()})
            await self.release(stop_if_running=True)

    async def watchdog(self):
        last_poll = 0.0
        while True:
            await asyncio.sleep(0.5)
            await self.check_heartbeat(time.time())
            if time.time() - last_poll > STATUS_POLL_S:
                last_poll = time.time()
                self.send_line("status")

    def start(self):
        raise NotImplementedError

    def send_line(self, line: str):
        raise NotImplementedError


# ----------------------------------------------------------------------------
# Real rig over USB serial
# ----------------------------------------------------------------------------
class SerialBridge(Bridge):
    def __init__(self, port: str | None, runs_dir: Path = RUNS_DIR):
        super().__init__(runs_dir)
        self.port = port
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._reader, daemon=True).start()

    def _open(self) -> serial.Serial | None:
        ports = [self.port] if self.port else [
            p.device for p in serial.tools.list_ports.comports()
            if any(h in (p.description or "").upper() for h in ("CP210", "CH340", "CH910", "USB", "UART"))]
        for dev in ports:
            try:
                s = serial.Serial()
                s.port, s.baudrate, s.timeout = dev, BAUD, 0.2
                s.dtr = s.rts = False             # don't reset the ESP32
                s.open()
            except serial.SerialException:
                continue
            if self.port:
                return s
            # Auto-detect: the rig answers "status"
            s.write(b"status\n")
            deadline = time.time() + 1.5
            buf = b""
            while time.time() < deadline:
                buf += s.read(512)
                if b"status " in buf or b"SWING" in buf or b"BAL " in buf:
                    return s
            s.close()
        return None

    def _reader(self):
        while True:
            s = self._open()
            if not s:
                self._set_state("OFFLINE")
                time.sleep(2)
                continue
            self.ser = s
            print(f"[bridge] connected to {s.port}")
            self.send_line("status")
            self.send_line("exp list")
            self.send_line("params")
            buf = b""
            try:
                while True:
                    buf += s.read(4096)
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        self.handle_line(raw.decode("utf-8", errors="replace"))
            except serial.SerialException as e:
                print(f"[bridge] serial lost: {e}")
                self.ser = None
                self.serial_lost()
                time.sleep(1)

    def send_line(self, line: str):
        with self.lock:
            if self.ser:
                try:
                    self.ser.write((line + "\n").encode())
                except serial.SerialException:
                    pass


# ----------------------------------------------------------------------------
# Simulated rig (same lines as the firmware) for building the frontend
# ----------------------------------------------------------------------------
MOCK_EXPERIMENTS = [("SwingBal", "Swing-up & balance"),
                    ("SignCheck", "Sign check (motor off)"),
                    ("StepTest", "Speed loop step test")]
# Mirrors PARAMS in cart_single.ino v11.1: (experiment or None = shared, name, default, lo, hi, unit, desc)
MOCK_PARAMS = [
    ("SwingBal", "BAL_MODE", 0, 0, 1, "-", "0 = gains from BAL_POLE, 1 = manual gains"),
    ("SwingBal", "BAL_POLE", 5, 3, 7, "rad/s", "balance stiffness (all four gains computed from it)"),
    ("SwingBal", "BAL_K_TH", 62.8, 0, 200, "m/s2/rad", "manual gain: pendulum angle"),
    ("SwingBal", "BAL_K_THD", 11.2, 0, 40, "m/s2/(rad/s)", "manual gain: pendulum angular speed"),
    ("SwingBal", "BAL_K_X", 19.9, -50, 100, "m/s2/m", "manual gain: cart position"),
    ("SwingBal", "BAL_K_V", 15.9, -50, 80, "m/s2/(m/s)", "manual gain: cart speed"),
    ("SwingBal", "CATCH_ANGLE_DEG", 14.3, 5, 20, "deg", "start balancing inside this angle from upright"),
    ("SwingBal", "CATCH_RATE", 2, 1, 3, "rad/s", "...and only if turning slower than this"),
    ("SwingBal", "DROP_ANGLE_DEG", 34.4, 20, 45, "deg", "give up balancing past this angle"),
    ("SwingBal", "SWING_V", 0.4, 0.1, 0.6, "m/s", "swing-up cart speed"),
    ("SwingBal", "SWING_E_TARGET", 0.02, -0.1, 0.2, "-", "energy target (0 = just reaches upright)"),
    ("SwingBal", "SWING_E_SLOW", 0.6, 0.2, 1.5, "-", "how gradually the push eases off near the top"),
    ("SwingBal", "SWING_PHASE_DEG", 15, 0, 90, "deg", "push timing lead (0 = at the bottom, 90 = at swing ends)"),
    ("SwingBal", "K_X_SWING", 1, 0, 3, "1/s", "pull back toward centre while swinging"),
    (None, "KV_P", 1.5, 0.5, 3, "1/(m/s)", "speed loop gain"),
    (None, "V_FF", 0.25, 0.1, 0.5, "1/(m/s)", "speed loop feed-forward"),
    (None, "V_MAX", 0.8, 0.2, 1.0, "m/s", "top cart speed"),
]
MOCK_RUN_STATE = {"SwingBal": "SWING", "SignCheck": "CHECK", "StepTest": "STEP"}


class MockBridge(Bridge):
    def __init__(self, runs_dir: Path = RUNS_DIR):
        super().__init__(runs_dir)
        self.vals = {p[1]: float(p[2]) for p in MOCK_PARAMS}
        self.sim_exp = MOCK_EXPERIMENTS[0][0]
        self.sim_state = "IDLE"
        self.t0 = 0.0          # when sim_state last changed
        self.run_t0 = 0.0      # D line time 0
        self.next_d = 0.0      # time of the next D line
        self.inbox: list[str] = []

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def send_line(self, line: str):
        self.inbox.append(line)

    def _emit(self, line: str):
        self.handle_line(line)

    def _visible(self):
        return [p for p in MOCK_PARAMS if p[0] in (None, self.sim_exp)]

    def _goto(self, state: str, now: float):
        self.sim_state, self.t0 = state, now

    def _cmd(self, line: str):
        parts = line.split()
        if not parts:
            return
        cmd = parts[0].lower()
        idle = self.sim_state == "IDLE"
        if cmd == "status":
            self._emit(f"status {self.sim_state} {self.sim_exp}")
        elif cmd == "exp":
            if len(parts) == 2 and parts[1] == "list":
                for name, title in MOCK_EXPERIMENTS:
                    self._emit(f"exp {name} | {title}")
                self._emit("exp end")
            elif len(parts) == 3 and parts[1] == "select":
                match = next((n for n, _ in MOCK_EXPERIMENTS if n.lower() == parts[2].lower()), None)
                if not idle:
                    self._emit(f"err exp busy ({self.sim_state})")
                elif match is None:
                    self._emit(f"err exp {parts[2]} unknown experiment")
                else:
                    self.sim_exp = match
                    self._emit(f"ok exp {match}")
            else:
                self._emit("err exp usage: exp list | exp select NAME")
        elif cmd == "params":
            for _exp, n, d, lo, hi, u, desc in self._visible():
                self._emit(f"param {n} {self.vals[n]:g} {lo:g} {hi:g} {d:g} {u} | {desc}")
            self._emit("params end")
        elif cmd == "stop":
            self._emit("ok stop")
            if not idle:
                self._emit("STOP triggered — motor stopped. Press START to re-home.")
                if self.sim_state not in ("HOMING", "HANG"):
                    self._emit("run end stop")
                self._goto("IDLE", time.time())
        elif cmd in ("home", "start", "run"):
            if not idle:
                self._emit(f"err home busy ({self.sim_state})")
                return
            self._emit("ok home")
            self._emit("Homing: driving left...")
            self._goto("HOMING", time.time())
        elif cmd == "defaults":
            if not idle:
                self._emit(f"err defaults busy ({self.sim_state})")
                return
            self.vals = {p[1]: float(p[2]) for p in MOCK_PARAMS}
            self._emit("ok defaults")
        elif cmd == "set" and len(parts) == 3:
            name, v = parts[1].upper(), parts[2]
            spec = next((p for p in self._visible() if p[1] == name), None)
            if not spec:
                self._emit(f"err {name} unknown setting")
            elif not idle:
                self._emit(f"err {name} busy ({self.sim_state}): change settings while idle")
            else:
                try:
                    f = float(v)
                except ValueError:
                    self._emit(f"err {name} not a number")
                    return
                if not (spec[3] <= f <= spec[4]):
                    self._emit(f"err {name} range {spec[3]:g}..{spec[4]:g}")
                else:
                    self.vals[name] = f
                    self._emit(f"ok {name} {f:g}")
        else:
            self._emit(f"err {cmd} unknown command")

    def _sample(self, t: float) -> tuple[float, float, float]:
        """x (m), continuous theta (rad, pi = hanging), u (V) at t s into the current state."""
        s = self.sim_state
        if s == "SWING":
            amp = math.radians(min(170, 20 + 22 * t))
            return 0.18 * math.sin(5.6 * t), math.pi + amp * math.sin(5.6 * t), 0.75 * math.cos(5.6 * t)
        if s == "BALANCE":
            th = math.radians(3 * math.exp(-t) * math.cos(6 * t) + random.gauss(0, 0.15))
            return 0.12 * math.exp(-t / 3) * math.cos(1.5 * t), th, random.gauss(0, 0.1)
        if s == "STEP":
            u = 0.6 if int(t / 0.6) % 2 == 0 else -0.6
            return 0.09 * math.sin(math.pi * t / 0.6), math.pi + 0.05 * math.sin(5.6 * t), u
        return 0.0, math.pi + 0.1 * math.sin(5.6 * t), 0.0        # CHECK: motor off

    def _begin_run(self, now: float):
        params = " ".join(f"{p[1]}={self.vals[p[1]]:g}" for p in self._visible())
        self._emit(f"run start {self.sim_exp} {params}")
        self.run_t0 = self.next_d = now
        state = MOCK_RUN_STATE[self.sim_exp]
        self._emit(f">> {state}")
        self._goto(state, now)

    def _run(self):
        self._emit("=== ESP32 Single-Board Pendulum Controller v11.1-single (MOCK) ===")
        self._emit('Ready. Waiting for START (button or "home") to home...')
        for cmd in ("status", "exp list", "params"):   # what SerialBridge asks on connect
            self._cmd(cmd)
        last = 0.0
        while True:
            while self.inbox:
                self._cmd(self.inbox.pop(0))
            now = time.time()
            t = now - self.t0
            s = self.sim_state
            if s == "HOMING" and t > 4:
                self._emit("Homing complete!")
                self._goto("HANG", now)
            elif s == "HANG" and t > 2.2:
                self._emit("Hanging reference: 12 (swing 170 counts)")
                self._begin_run(now)
            elif s == "SWING" and t > 7:
                self._emit(">> BALANCE")
                self._goto("BALANCE", now)
            elif s == "STEP" and t > 3.6:
                self._emit("Step test done.")
                self._emit("run end done")
                self._goto("IDLE", now)
            if self.sim_state in ("SWING", "BALANCE", "STEP", "CHECK"):
                while self.next_d <= now:            # 500 Hz, like the firmware
                    x, th, u = self._sample(self.next_d - self.t0)
                    self._emit(f"D {int((self.next_d - self.run_t0) * 1000)} {x:.4f} {th:.4f} {u:.3f} nan")
                    self.next_d += 0.002
            period = 0.02 if self.sim_state == "BALANCE" else 0.1
            if now - last >= period:
                last = now
                t = now - self.t0
                if self.sim_state == "HANG":
                    self._emit(f"HANG  pend {int(85 * math.sin(5.6 * t))}  swing 170")
                elif self.sim_state == "SWING":
                    amp = min(170, 20 + 22 * t)
                    th = ((180 + amp * math.sin(5.6 * t)) + 180) % 360 - 180
                    w = amp / 57.3 * 5.6 * math.cos(5.6 * t)
                    x = 180 * math.sin(5.6 * t)
                    E = -2 + 2 * min(1, t / 7)
                    self._emit(f"SWING th {th:7.1f} deg  w {w:6.2f}  x {x:6.1f} mm  v {0.4 * math.cos(5.6 * t):6.3f}  "
                               f"vr {0.4 * math.copysign(1, math.cos(5.6 * t)):5.2f}  E {E:5.2f}  Eend {E:5.2f}  u {0.3:5.2f}")
                elif self.sim_state == "BALANCE":
                    th = 3 * math.exp(-t) * math.cos(6 * t) + random.gauss(0, 0.15)
                    x = 120 * math.exp(-t / 3) * math.cos(1.5 * t) + random.gauss(0, 1)
                    trim = 0.3 * (1 - math.exp(-max(0, t - 2) / 8))
                    self._emit(f"BAL   th {th:7.1f} deg  w {random.gauss(0, 0.1):6.2f}  x {x:6.1f} mm  v {random.gauss(0, 0.02):6.3f}  "
                               f"vr {0.0:5.2f}  trim {trim:5.2f} deg  u {random.gauss(0, 0.05):5.2f}")
            time.sleep(0.005)


# ----------------------------------------------------------------------------
# Web server
# ----------------------------------------------------------------------------
def _authorised(request: Request) -> bool:
    return not TOKEN or request.query_params.get("token") == TOKEN


def make_app(bridge: Bridge) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        bridge.loop = asyncio.get_running_loop()
        bridge.start()
        task = asyncio.ensure_future(bridge.watchdog())
        yield
        task.cancel()

    app = FastAPI(lifespan=lifespan)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        if TOKEN and ws.query_params.get("token") != TOKEN:
            await ws.close(code=4401)
            return
        await ws.accept()
        bridge.clients.add(ws)
        await ws.send_text(json.dumps(bridge.snapshot(ws)))
        await bridge.announce_control()
        try:
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await bridge.command(ws, msg)
        except WebSocketDisconnect:
            pass
        finally:
            await bridge.drop(ws)

    @app.get("/runs")
    async def list_runs(request: Request):
        if not _authorised(request):
            return JSONResponse({"error": "token required"}, status_code=401)
        out = []
        for p in sorted(bridge.runs_dir.glob("*.json"), reverse=True)[:200]:
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    @app.get("/runs/{file}")
    async def get_run(file: str, request: Request):
        if not _authorised(request):
            return JSONResponse({"error": "token required"}, status_code=401)
        if not RUN_FILE.fullmatch(file):
            return JSONResponse({"error": "not found"}, status_code=404)
        path = (bridge.runs_dir / file).resolve()
        if path.parent != bridge.runs_dir.resolve() or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path, filename=file)

    @app.get("/")
    async def index():
        return FileResponse(FRONTEND / "index.html")

    app.mount("/", StaticFiles(directory=FRONTEND), name="static")
    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port of the rig's ESP32 (default: auto-detect)")
    ap.add_argument("--mock", action="store_true", help="simulate the rig")
    ap.add_argument("--host", default="127.0.0.1", help="listen address (127.0.0.1 = this PC only)")
    ap.add_argument("--http-port", type=int, default=8000)
    a = ap.parse_args()
    bridge = MockBridge() if a.mock else SerialBridge(a.port)
    print(f"[bridge] {'MOCK rig' if a.mock else 'serial rig'} — open http://{a.host}:{a.http_port}")
    uvicorn.run(make_app(bridge), host=a.host, port=a.http_port, log_level="warning")


if __name__ == "__main__":
    main()
