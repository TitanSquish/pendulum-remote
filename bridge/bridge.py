"""Pendulum remote bridge: ESP32 #2 (USB serial) <-> browsers (WebSocket).

    python bridge/bridge.py                 # auto-detect the motor board's port
    python bridge/bridge.py --port COM3     # or name it
    python bridge/bridge.py --mock          # simulated rig, no hardware needed

Then open http://localhost:8000

- Reads the motor board's serial output (firmware v10.1+), parses telemetry,
  state changes, settings and replies, and pushes them to every browser.
- Forwards start / stop / set / defaults from the browser to the board.
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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
TOKEN = os.environ.get("BRIDGE_TOKEN", "")
HEARTBEAT_TIMEOUT_S = 3.0
STATUS_POLL_S = 2.0

# ----------------------------------------------------------------------------
# Parsing the firmware's serial lines
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
                  "START from", "Homing: driving left", "=== ESP32", "Ready.", "WARNING")
STATE_NAMES = {"STEP TEST": "STEP"}


class Bridge:
    """Shared state + the browser side. Subclasses provide send_line()."""

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.controller: WebSocket | None = None
        self.last_beat = 0.0
        self.state = "OFFLINE"
        self.params: list[dict] = []
        self._param_buf: list[dict] = []
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
                "control": self._role(ws), "clients": len(self.clients)}

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
            self._set_state(line[7:].strip())
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
            if ok and len(words) > 1 and words[1] not in ("stop", "home"):
                self.send_line("params")     # a setting changed: refresh the values
        else:
            if line.startswith(EVENT_PREFIXES):
                self.push({"kind": "event", "text": line, "t": time.time()})
            if line.startswith(("STOP triggered", "Stopped at", "ERROR", "LIMIT HIT")) or \
               line.startswith("Ready.") or "aborted" in line:
                self.send_line("status")
        self.push({"kind": "log", "text": line})

    def _set_state(self, s: str):
        s = STATE_NAMES.get(s, s)
        if s != self.state:
            self.state = s
            self.push({"kind": "state", "state": s})

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
            self.send_line("home")
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

    async def watchdog(self):
        """Stop the rig if the controller stops sending heartbeats; poll status."""
        last_poll = 0.0
        while True:
            await asyncio.sleep(0.5)
            if self.controller and time.time() - self.last_beat > HEARTBEAT_TIMEOUT_S:
                self.push({"kind": "event", "text": "Controller heartbeat lost — stopping.",
                           "t": time.time()})
                await self.release(stop_if_running=True)
            if time.time() - last_poll > STATUS_POLL_S:
                last_poll = time.time()
                self.send_line("status")

    def send_line(self, line: str):
        raise NotImplementedError


# ----------------------------------------------------------------------------
# Real rig over USB serial
# ----------------------------------------------------------------------------
class SerialBridge(Bridge):
    def __init__(self, port: str | None):
        super().__init__()
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
                s.port, s.baudrate, s.timeout = dev, 115200, 0.2
                s.dtr = s.rts = False             # don't reset the ESP32
                s.open()
            except serial.SerialException:
                continue
            if self.port:
                return s
            # Auto-detect: the motor board answers "status"
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
                self._set_state("OFFLINE")
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
MOCK_PARAMS = [  # mirrors the PARAMS table in cart_control.ino v10.1
    ("BAL_MODE", 0, 0, 1, "-", "0 = gains from BAL_POLE, 1 = manual gains"),
    ("BAL_POLE", 5, 3, 7, "rad/s", "balance stiffness (all four gains computed from it)"),
    ("BAL_K_TH", 62.8, 0, 200, "m/s2/rad", "manual gain: pendulum angle"),
    ("BAL_K_THD", 11.2, 0, 40, "m/s2/(rad/s)", "manual gain: pendulum angular speed"),
    ("BAL_K_X", 19.9, -50, 100, "m/s2/m", "manual gain: cart position"),
    ("BAL_K_V", 15.9, -50, 80, "m/s2/(m/s)", "manual gain: cart speed"),
    ("CATCH_ANGLE_DEG", 14.3, 5, 20, "deg", "start balancing inside this angle from upright"),
    ("CATCH_RATE", 2, 1, 3, "rad/s", "...and only if turning slower than this"),
    ("DROP_ANGLE_DEG", 34.4, 20, 45, "deg", "give up balancing past this angle"),
    ("SWING_V", 0.4, 0.1, 0.6, "m/s", "swing-up cart speed"),
    ("SWING_E_TARGET", 0.02, -0.1, 0.2, "-", "energy target (0 = just reaches upright)"),
    ("SWING_E_SLOW", 0.6, 0.2, 1.5, "-", "how gradually the push eases off near the top"),
    ("SWING_PHASE_DEG", 15, 0, 90, "deg", "push timing lead (0 = at the bottom, 90 = at swing ends)"),
    ("K_X_SWING", 1, 0, 3, "1/s", "pull back toward centre while swinging"),
    ("KV_P", 1.5, 0.5, 3, "1/(m/s)", "speed loop gain"),
    ("V_FF", 0.25, 0.1, 0.5, "1/(m/s)", "speed loop feed-forward"),
    ("V_MAX", 0.8, 0.2, 1.0, "m/s", "top cart speed"),
]


class MockBridge(Bridge):
    def __init__(self):
        super().__init__()
        self.vals = {p[0]: float(p[1]) for p in MOCK_PARAMS}
        self.sim_state = "IDLE"
        self.t0 = 0.0
        self.inbox: list[str] = []

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def send_line(self, line: str):
        self.inbox.append(line)

    def _emit(self, line: str):
        self.handle_line(line)

    def _cmd(self, line: str):
        parts = line.split()
        cmd = parts[0].lower()
        idle = self.sim_state == "IDLE"
        if cmd == "status":
            self._emit(f"status {self.sim_state}")
        elif cmd == "params":
            for n, d, lo, hi, u, desc in MOCK_PARAMS:
                self._emit(f"param {n} {self.vals[n]:g} {lo:g} {hi:g} {d:g} {u} | {desc}")
            self._emit("params end")
        elif cmd == "stop":
            self._emit("ok stop")
            if self.sim_state != "IDLE":
                self._emit("STOP triggered — motor stopped. Press START to re-home.")
                self.sim_state = "IDLE"
        elif cmd == "home":
            if not idle:
                self._emit(f"err home busy ({self.sim_state})")
                return
            self._emit("ok home")
            self._emit("Homing: driving left...")
            self.sim_state, self.t0 = "HOMING", time.time()
        elif cmd == "defaults":
            if not idle:
                self._emit(f"err defaults busy ({self.sim_state})")
                return
            self.vals = {p[0]: float(p[1]) for p in MOCK_PARAMS}
            self._emit("ok defaults")
        elif cmd == "set" and len(parts) == 3:
            name, v = parts[1].upper(), parts[2]
            spec = next((p for p in MOCK_PARAMS if p[0] == name), None)
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
                if not (spec[2] <= f <= spec[3]):
                    self._emit(f"err {name} range {spec[2]:g}..{spec[3]:g}")
                else:
                    self.vals[name] = f
                    self._emit(f"ok {name} {f:g}")
        else:
            self._emit(f"err {cmd} unknown command")

    def _run(self):
        self._emit("=== ESP32 #2 Motor Control v10.1 (MOCK) ===")
        self._emit("Ready. Waiting for START (button or Pi GPIO 4) to home...")
        self._cmd("status")
        self._cmd("params")
        last = 0.0
        while True:
            while self.inbox:
                self._cmd(self.inbox.pop(0))
            now = time.time()
            t = now - self.t0
            s = self.sim_state
            if s == "HOMING" and t > 4:
                self._emit("Homing complete!")
                self._emit(">> HANG")
                self.sim_state, self.t0 = "HANG", now
            elif s == "HANG" and t > 2.2:
                self._emit("Hanging reference: 12 (swing 170 counts)")
                self._emit(">> SWING")
                self.sim_state, self.t0 = "SWING", now
            elif s == "SWING" and t > 7:
                self._emit(">> BALANCE")
                self.sim_state, self.t0 = "BALANCE", now
            period = 0.02 if self.sim_state == "BALANCE" else 0.1
            if now - last >= period:
                last = now
                t = now - self.t0
                if self.sim_state == "HANG":
                    self._emit(f"HANG  pend {int(85 * math.sin(5.6 * t))}  swing 170")
                elif self.sim_state == "SWING":
                    amp = min(170, 20 + 22 * t)                  # swing grows (deg from hanging)
                    th = ((180 + amp * math.sin(5.6 * t)) + 180) % 360 - 180
                    w = amp / 57.3 * 5.6 * math.cos(5.6 * t)
                    x = 180 * math.sin(5.6 * t)
                    E = -2 + 2 * min(1, t / 7)
                    self._emit(f"SWING th {th:7.1f} deg  w {w:6.2f}  x {x:6.1f} mm  v {0.4 * math.cos(5.6 * t):6.3f}  "
                               f"vr {0.4 * math.copysign(1, math.cos(5.6 * t)):5.2f}  E {E:5.2f}  Eend {E:5.2f}  u {0.3:5.2f}")
                elif self.sim_state == "BALANCE":
                    th = 3 * math.exp(-t) * math.cos(6 * t) + random.gauss(0, 0.15)
                    x = 120 * math.exp(-t / 3) * math.cos(1.5 * t) + random.gauss(0, 1)
                    trim = 5.5 * (1 - math.exp(-max(0, t - 2) / 8))
                    self._emit(f"BAL   th {th:7.1f} deg  w {random.gauss(0, 0.1):6.2f}  x {x:6.1f} mm  v {random.gauss(0, 0.02):6.3f}  "
                               f"vr {0.0:5.2f}  trim {trim:5.2f} deg  u {random.gauss(0, 0.05):5.2f}")
            time.sleep(0.005)


# ----------------------------------------------------------------------------
# Web server
# ----------------------------------------------------------------------------
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

    @app.get("/")
    async def index():
        return FileResponse(FRONTEND / "index.html")

    app.mount("/", StaticFiles(directory=FRONTEND), name="static")
    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port of the motor ESP32 (default: auto-detect)")
    ap.add_argument("--mock", action="store_true", help="simulate the rig")
    ap.add_argument("--host", default="127.0.0.1", help="listen address (127.0.0.1 = this PC only)")
    ap.add_argument("--http-port", type=int, default=8000)
    a = ap.parse_args()
    bridge = MockBridge() if a.mock else SerialBridge(a.port)
    print(f"[bridge] {'MOCK rig' if a.mock else 'serial rig'} — open http://{a.host}:{a.http_port}")
    uvicorn.run(make_app(bridge), host=a.host, port=a.http_port, log_level="warning")


if __name__ == "__main__":
    main()
