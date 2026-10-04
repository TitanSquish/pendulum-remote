# Pendulum Lab: remote control (test setup)

A standalone remote-control console for the ED2 inverted pendulum, kept separate
from the main ED2-Repo website so it can be tested without touching the live site.

```
Browser (frontend/)  ⇅ WebSocket  bridge/bridge.py (this PC)  ⇅ USB serial  ESP32 #2
```

## Requirements

- Motor ESP32 flashed with **cart_control v10.1** or later (adds the serial
  commands and online parameters). Encoder ESP32 on v9.2.
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

- **Start** homes the cart, then swings the pendulum up and balances it.
- **Stop** (or the **Esc** key) stops the rig. Anyone connected can press it, even without control.
- **Parameters** can be changed while the rig is idle. The ESP32 enforces every
  limit itself, so the page can't push it outside a safe range even if bypassed.
  Gain mode switches between pole placement (one stiffness value) and four manual gains.
- Live view of the cart and pendulum, angle and cart-position charts, and an activity log.

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

## Serial protocol (ESP32 #2, v10.1)

One command per line at 115200 baud. Replies start with `ok `, `err `, `param `,
`params end` or `status `.

| Command | Effect |
|---|---|
| `home` | Start (only while idle) |
| `stop` | Stop now, also during homing |
| `status` | `status IDLE`, `HOMING`, `HANG`, `SWING`, `BALANCE`, `BRAKE`, ... |
| `params` | `param NAME VALUE MIN MAX DEFAULT UNIT \| description` per setting, then `params end` |
| `set NAME VALUE` | Change a setting (idle only, within limits) |
| `defaults` | Restore the firmware defaults |

Telemetry lines (`SWING ...` at 10 Hz, `BAL ...` at 50 Hz) and event lines are
documented in `ED2-Repo/docs/esp32.md`.

## Files

- `bridge/bridge.py`: serial ⇄ WebSocket bridge, control lock, heartbeat watchdog, mock rig
- `frontend/`: plain HTML/CSS/JS console (no build step)
