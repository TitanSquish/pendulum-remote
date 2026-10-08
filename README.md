# Pendulum Lab: remote control (test setup)

A standalone remote-control console for the ED2 inverted pendulum, kept separate
from the main ED2-Repo website so it can be tested without touching the live site.

```
Browser (frontend/)  ⇅ WebSocket  bridge/bridge.py (this PC)  ⇅ USB serial  rig ESP32
```

## Requirements

- Rig ESP32 flashed with **cart_single v11.1** or later (`ED2-Repo/cart/cart_single`):
  experiments, 921600 baud, run data lines.
- Python 3.10+ and `pip install -r bridge/requirements.txt`

## Run it

```bash
# Simulated rig: no hardware needed, for working on the frontend
python bridge/bridge.py --mock

# Real rig: close the Arduino serial monitor first (only one program can open the port)
python bridge/bridge.py              # auto-detects the motor board
python bridge/bridge.py --port COM3  # or name the port
```

Open http://localhost:8000, press **Take control**, then **Start**.

## What the page does

- **Experiment** picker: choose what the next run does (swing-up & balance,
  rig checks; the Feedback lab experiments arrive in later stages).
- **Start** homes the cart, measures the hanging pendulum, then runs the experiment.
- **Stop** (or the **Esc** key) stops the rig. Anyone connected can press it, even without control.
- **Parameters** for the selected experiment can be changed while the rig is idle.
  The ESP32 enforces every limit itself, so the page can't push it outside a safe range even if bypassed.
- Live view of the cart and pendulum, and angle, cart-position and motor-command charts.
- **Runs**: every finished run is saved in `runs/` on this PC as CSV and MATLAB
  `.mat` (`t x theta u ref`, plus `simout = [x theta u t ref]`, `params`, `results`).
  Download them from the page.

## Safety built into the bridge

- One controller at a time. Others can watch and press Stop.
- If the controlling browser disconnects, or stops sending its once-a-second
  heartbeat for 3 s, while the rig is running, the bridge sends `stop`.
- The bridge listens on `127.0.0.1` only (this PC) unless you pass `--host`.

Before putting it online:

1. Keep a physical stop or power switch within reach of the rig.
2. Set a password: `BRIDGE_TOKEN=some-long-secret python bridge/bridge.py`,
   then open `http://host:8000/?token=some-long-secret`.
3. Expose it through a tunnel with access control (for example Cloudflare Tunnel
   with Cloudflare Access) rather than opening a port on the router.

## Serial protocol (cart_single v11.1, 921600 baud)

One command per line. Replies start with `ok `, `err `, `param `, `params end`,
`status `, `exp ` or `io `.

| Command | Effect |
|---|---|
| `exp list` | `exp NAME \| title` per experiment, then `exp end` |
| `exp select NAME` | Experiment for the next run (idle only) |
| `run` (or `home`) | Home, measure the hanging reference, run the selected experiment |
| `stop` | Stop now, also during homing |
| `status` | `status STATE EXPERIMENT` |
| `params` | Shared + the selected experiment's settings: `param NAME VALUE MIN MAX DEFAULT UNIT \| description`, then `params end` |
| `set NAME VALUE` | Change a setting (idle only, within limits) |
| `defaults` | Restore the firmware defaults |

During a run the board prints `run start NAME K=V ...`, then one
`D <t_ms> <x_m> <theta_rad> <u_V> <ref>` line per 2 ms step (theta continuous:
0 = upright, π = hanging; `ref` is `nan` without a setpoint), then
`run end REASON` (`done`, `stop`, `soft-limit`, `overspeed`, `limit-switch`).
Human-readable `SWING` / `BAL` / `HANG` lines and `>> STATE` lines are
documented in `ED2-Repo/docs/esp32.md`.

## Tests

    python -m pip install -r bridge/requirements-dev.txt
    python -m pytest bridge/tests -q

## Files

- `bridge/bridge.py`: serial ⇄ WebSocket bridge, control lock, heartbeat watchdog, run downloads, mock rig
- `bridge/runs.py`: turns `run start` / `D` / `run end` lines into saved runs (CSV, .mat, .json)
- `bridge/matfile.py`: small MATLAB Level 5 .mat writer (no SciPy needed to run the bridge)
- `bridge/tests/`: pytest suite (needs `requirements-dev.txt`)
- `frontend/`: plain HTML/CSS/JS console (no build step)
- `runs/`: saved runs (gitignored)
