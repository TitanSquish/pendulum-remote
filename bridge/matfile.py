"""Minimal MATLAB Level 5 MAT-file writer, so the bridge needs no SciPy.

Writes double column vectors and matrices, 1x1 structs of doubles and char
row vectors: enough for run files. MATLAB's load() and scipy.io.loadmat read
them. Format: MathWorks "MAT-File Format" (Level 5), little-endian.
"""
from __future__ import annotations

import re
import struct
import time
from pathlib import Path

MI_INT8, MI_UINT16, MI_INT32, MI_UINT32, MI_DOUBLE, MI_MATRIX = 1, 4, 5, 6, 9, 14
MX_STRUCT, MX_CHAR, MX_DOUBLE = 2, 4, 6
FIELD_NAME_LEN = 32                  # 31 characters + NUL


def _element(mtype: int, data: bytes) -> bytes:
    """Data element; small-element format for <= 4 bytes (as MATLAB writes it)."""
    if len(data) <= 4:
        return struct.pack("<HH", mtype, len(data)) + data.ljust(4, b"\0")
    return struct.pack("<II", mtype, len(data)) + data + b"\0" * (-len(data) % 8)


def _matrix(name: str, cls: int, dims: tuple[int, ...], body: bytes) -> bytes:
    inner = (_element(MI_UINT32, struct.pack("<II", cls, 0))
             + _element(MI_INT32, struct.pack(f"<{len(dims)}i", *dims))
             + _element(MI_INT8, name.encode("ascii"))
             + body)
    return struct.pack("<II", MI_MATRIX, len(inner)) + inner


def _doubles(name: str, dims: tuple[int, int], values: list[float]) -> bytes:
    return _matrix(name, MX_DOUBLE, dims,
                   _element(MI_DOUBLE, struct.pack(f"<{len(values)}d", *values)))


def _chars(name: str, text: str) -> bytes:
    codes = [ord(c) if ord(c) < 0x10000 else ord("?") for c in text]
    return _matrix(name, MX_CHAR, (1, len(codes)),
                   _element(MI_UINT16, struct.pack(f"<{len(codes)}H", *codes)))


def matlab_name(name: str) -> str:
    """A valid MATLAB identifier: letters, digits, _; starts with a letter; <= 31 chars."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not s or not s[0].isalpha():
        s = "x" + s
    return s[:31]


def _struct(name: str, fields: dict[str, float]) -> bytes:
    names: list[str] = []
    for key in fields:
        n = matlab_name(key)
        while n in names:                                  # keep field names unique
            n = (n[:27] + f"_{len(names)}")[:31]
        names.append(n)
    body = _element(MI_INT32, struct.pack("<i", FIELD_NAME_LEN))
    body += _element(MI_INT8, b"".join(n.encode("ascii").ljust(FIELD_NAME_LEN, b"\0") for n in names))
    for value in fields.values():
        body += _doubles("", (1, 1), [float(value)])
    return _matrix(matlab_name(name), MX_STRUCT, (1, 1), body)


def write_mat(path: Path,
              columns: dict[str, list[float]] | None = None,
              matrices: dict[str, list[list[float]]] | None = None,
              structs: dict[str, dict[str, float]] | None = None,
              strings: dict[str, str] | None = None) -> None:
    text = f"MATLAB 5.0 MAT-file, Platform: pendulum-remote, Created on: {time.asctime()}"
    out = [text.encode("ascii").ljust(116, b" ")[:116], b"\0" * 8, struct.pack("<H", 0x0100), b"IM"]
    for name, col in (columns or {}).items():
        out.append(_doubles(matlab_name(name), (len(col), 1), [float(v) for v in col]))
    for name, cols in (matrices or {}).items():
        rows = len(cols[0]) if cols else 0
        flat = [float(v) for c in cols for v in c]          # column-major
        out.append(_doubles(matlab_name(name), (rows, len(cols)), flat))
    for name, fields in (structs or {}).items():
        out.append(_struct(name, fields))
    for name, text_value in (strings or {}).items():
        out.append(_chars(matlab_name(name), text_value))
    Path(path).write_bytes(b"".join(out))
