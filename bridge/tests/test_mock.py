from bridge import MockBridge


class QuietMock(MockBridge):
    """MockBridge without the browser side: pushes are collected, not broadcast."""

    def __init__(self, runs_dir):
        super().__init__(runs_dir)
        self.pushed = []

    def push(self, msg):
        self.pushed.append(msg)


def test_mock_lists_the_stage2_experiments_in_firmware_order(tmp_path):
    m = QuietMock(tmp_path)
    m._cmd("exp list")
    assert [e["name"] for e in m.experiments] == ["SwingBal", "FreeSwing", "Friction", "SignCheck", "StepTest"]


def test_mock_friction_shows_its_settings_and_the_shared_ones(tmp_path):
    m = QuietMock(tmp_path)
    m._cmd("exp select Friction")
    m._cmd("params")
    assert {p["name"] for p in m.params} == {"FR_RAMP_VPS", "FRIC_POS_V", "FRIC_NEG_V", "KV_P", "V_FF", "V_MAX"}


def test_mock_freeswing_shows_its_duration(tmp_path):
    m = QuietMock(tmp_path)
    m._cmd("exp select FreeSwing")
    m._cmd("params")
    assert "FS_DURATION_S" in {p["name"] for p in m.params}
