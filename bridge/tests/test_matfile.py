import math

import pytest

sio = pytest.importorskip("scipy.io")

from matfile import matlab_name, write_mat


def test_columns_matrix_struct_and_strings(tmp_path):
    p = tmp_path / "a.mat"
    write_mat(p,
              columns={"t": [0.0, 0.002, 0.004], "ref": [math.nan] * 3},
              matrices={"simout": [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]},
              structs={"params": {"P": 27.84, "BAL_MODE": 0.0}},
              strings={"experiment": "CartPID"})
    m = sio.loadmat(p)
    assert m["t"].shape == (3, 1) and m["t"][1, 0] == 0.002
    assert math.isnan(m["ref"][0, 0])
    assert m["simout"].shape == (3, 2) and m["simout"][2, 1] == 6.0 and m["simout"][0, 1] == 4.0
    params = m["params"][0, 0]
    assert params["P"][0, 0] == 27.84 and params["BAL_MODE"][0, 0] == 0.0
    assert m["experiment"][0] == "CartPID"


def test_empty_run_and_odd_names(tmp_path):
    p = tmp_path / "b.mat"
    write_mat(p,
              columns={"t": []},
              matrices={"simout": [[], [], [], [], []]},
              structs={"params": {"2X": 1.0, "A-B": 2.0, "L" * 40: 3.0}, "results": {}},
              strings={"reason": ""})
    m = sio.loadmat(p)
    assert m["t"].shape == (0, 1)
    assert m["simout"].shape == (0, 5)
    assert set(m["params"].dtype.names) == {"x2X", "A_B", "L" * 31}
    assert "results" in m and "reason" in m


def test_matlab_name():
    assert matlab_name("BAL_POLE") == "BAL_POLE"
    assert matlab_name("2X") == "x2X"
    assert matlab_name("") == "x"
    assert len(matlab_name("a" * 50)) == 31
