"""Run recording for the experiment protocol (cart_single firmware v11.1+).

The firmware brackets every run with "run start NAME K=V ..." and
"run end REASON" and prints one data line per 2 ms control step in between:

    D <t_ms> <x_m> <theta_rad> <u_V> <ref> [r]

Recorder turns those lines into Run objects; save_run() writes a finished run
as CSV and MATLAB .mat files plus a small .json summary for the console.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from matfile import write_mat

BASE_COLUMNS = ("t", "x", "theta", "u", "ref")
EXTRA_COLUMNS = ("r",)               # optional trailing fields, in order
RUN_START = re.compile(r"^run start (\S+)(.*)$")
RUN_END = re.compile(r"^run end (\S+)")
RESULT = re.compile(r"^result (\S+) (\S+)")


def parse_d_line(line: str) -> tuple[float, ...] | None:
    """Data line -> (t_s, x, theta, u, ref, *extra), or None if malformed."""
    parts = line.split()
    if parts[:1] != ["D"] or not 6 <= len(parts) <= 6 + len(EXTRA_COLUMNS):
        return None
    try:
        vals = [float(p) for p in parts[1:]]
    except ValueError:
        return None
    if not all(math.isfinite(v) for v in vals[:4]):    # t, x, theta, u; ref may be nan
        return None
    vals[0] /= 1000.0                                    # ms -> s
    return tuple(vals)


def parse_run_start(line: str) -> tuple[str, dict[str, float]] | None:
    m = RUN_START.match(line)
    if not m:
        return None
    params: dict[str, float] = {}
    for tok in m.group(2).split():
        key, sep, val = tok.partition("=")
        if not sep:
            continue
        try:
            f = float(val)
        except ValueError:
            continue
        if math.isfinite(f):
            params[key] = f
    return m.group(1), params


@dataclass
class Run:
    name: str
    params: dict[str, float]
    started: float                       # wall clock, time.time()
    rows: list[tuple[float, ...]] = field(default_factory=list)
    results: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    dropped: int = 0                     # malformed "D" lines

    @property
    def columns(self) -> list[str]:
        width = max((len(r) for r in self.rows), default=len(BASE_COLUMNS))
        return list(BASE_COLUMNS) + list(EXTRA_COLUMNS[: width - len(BASE_COLUMNS)])

    def column(self, i: int) -> list[float]:
        return [r[i] if i < len(r) else math.nan for r in self.rows]

    @property
    def duration(self) -> float:
        return self.rows[-1][0] if self.rows else 0.0

    @property
    def stem(self) -> str:
        stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(self.started))
        return f"{stamp}_{re.sub(r'[^A-Za-z0-9_-]', '_', self.name)}"


class Recorder:
    """Feed every serial line to on_line(); it returns a list of events:
    ("start", Run), ("sample", row), ("result", (key, value)), ("end", Run)."""

    def __init__(self):
        self.run: Run | None = None

    def on_line(self, line: str, now: float | None = None) -> list[tuple[str, object]]:
        if line.startswith("D "):
            if self.run is None:
                return []
            row = parse_d_line(line)
            if row is None:
                self.run.dropped += 1
                return []
            self.run.rows.append(row)
            return [("sample", row)]
        start = parse_run_start(line)
        if start:
            events: list[tuple[str, object]] = []
            if self.run is not None:                     # no "run end": board reset mid-run
                events.append(("end", self._close("interrupted")))
            self.run = Run(start[0], start[1], time.time() if now is None else now)
            events.append(("start", self.run))
            return events
        m = RUN_END.match(line)
        if m:
            return [("end", self._close(m.group(1)))] if self.run is not None else []
        m = RESULT.match(line)
        if m and self.run is not None:
            try:
                val = float(m.group(2))
            except ValueError:
                return []
            if not math.isfinite(val):
                return []
            self.run.results[m.group(1)] = val
            return [("result", (m.group(1), val))]
        return []

    def abort(self, reason: str) -> Run | None:
        """Close an open run that will never get its "run end" (serial lost)."""
        return self._close(reason) if self.run is not None else None

    def _close(self, reason: str) -> Run:
        run, self.run = self.run, None
        run.reason = reason
        return run


def run_summary(run: Run, files: dict[str, str]) -> dict:
    return {"name": run.name, "started": run.started, "duration": run.duration,
            "reason": run.reason, "samples": len(run.rows), "dropped": run.dropped,
            "params": run.params, "results": run.results, "files": files}


def _fmt(v: float) -> str:
    return "nan" if math.isnan(v) else repr(float(v))


def save_run(run: Run, folder: Path) -> dict:
    """Write <stem>.csv, <stem>.mat and <stem>.json; return the summary."""
    folder.mkdir(parents=True, exist_ok=True)
    stem, n = run.stem, 2
    while (folder / f"{stem}.csv").exists():               # never overwrite a run
        stem, n = f"{run.stem}_{n}", n + 1
    cols = run.columns

    csv_path = folder / f"{stem}.csv"
    with open(csv_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# experiment {run.name}\n")
        f.write(f"# started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run.started))}\n")
        f.write(f"# end {run.reason}\n")
        if run.dropped:
            f.write(f"# dropped_lines {run.dropped}\n")
        for k, v in run.params.items():
            f.write(f"# param {k} {v:g}\n")
        for k, v in run.results.items():
            f.write(f"# result {k} {v:g}\n")
        f.write(",".join(cols) + "\n")
        for r in run.rows:
            f.write(",".join(_fmt(r[i]) if i < len(r) else "nan" for i in range(len(cols))) + "\n")

    data = {c: run.column(i) for i, c in enumerate(cols)}
    mat_path = folder / f"{stem}.mat"
    write_mat(mat_path,
              columns=data,
              matrices={"simout": [data["x"], data["theta"], data["u"], data["t"], data["ref"]]},
              structs={"params": run.params, "results": run.results},
              strings={"experiment": run.name, "reason": run.reason})

    summary = run_summary(run, {"csv": csv_path.name, "mat": mat_path.name})
    (folder / f"{stem}.json").write_text(json.dumps(summary), encoding="utf-8")
    return summary
