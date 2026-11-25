"""Board settings from Settings > Imaging Console > MaRCoS boards, and how scans and EMI use them."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sequences.common.pydanticConfig import BoardParameters, Config, MarcosParameters, configCreator
import external.seq.adjustments_acq.config as cfg
from services.api import emi

SCANNER = "10.42.0.251"


def layout(*boards):
    return cfg.board_layout([BoardParameters(**b) for b in boards], SCANNER, 11111)


BOARD0 = dict(rx0="mri", tx0=True)


def test_default_is_the_original_single_board():
    boards = MarcosParameters().boards
    assert [b.model_dump() for b in boards] == [
        dict(ip="", port=11111, rx0="mri", rx1="off", tx0=True, tx1=False, set_cic_shift=False)
    ]
    result = cfg.board_layout(boards, SCANNER, 11111)
    assert result["MARCOS_BOARDS"] is None
    assert result["MARCOS_BOARD_ADDRESSES"] == {0: dict(ip=SCANNER, port=11111)}
    assert result["MARCOS_RX_ROLES"] == {(0, 0): "mri"}
    assert result["MARCOS_TX_CHANNELS"] == (0,)
    assert result["MARCOS_SINGLE_BOARD_RX"] == 0
    assert result["MARCOS_BOARD_PROBLEMS"] == []


def test_config_files_without_boards_load_the_default():
    saved = configCreator().model_dump(mode="json")
    del saved["marcos_parameters"]["boards"]
    assert Config(**saved).marcos_parameters.boards == MarcosParameters().boards


def test_board0_always_keeps_a_tx_output():
    params = MarcosParameters(boards=[dict(rx0="mri", tx0=False, tx1=False)])
    assert params.boards[0].tx0 is True
    assert MarcosParameters(boards=[]).boards == MarcosParameters().boards


def test_two_boards_for_fid_with_tx1_and_cic_shift():
    result = layout(
        dict(rx0="mri", rx1="emi", tx0=False, tx1=True),
        dict(ip=" 169.254.93.61 ", rx0="mri", rx1="emi", set_cic_shift=True),
    )
    assert result["MARCOS_BOARDS"] == {
        0: dict(ip=SCANNER, port=11111, rx_channels=(0,)),
        1: dict(ip="169.254.93.61", port=11111, set_cic_shift=True, rx_channels=(0,)),
    }
    assert result["MARCOS_TX_CHANNELS"] == (1,)
    assert result["MARCOS_RX_ROLES"] == {(0, 0): "mri", (0, 1): "emi", (1, 0): "mri", (1, 1): "emi"}
    assert result["MARCOS_SINGLE_BOARD_RX"] == 0


def test_board0_rx1_alone_uses_the_board_list_and_single_board_rx1():
    result = layout(dict(rx0="off", rx1="mri", tx0=True))
    assert result["MARCOS_BOARDS"] == {0: dict(ip=SCANNER, port=11111, rx_channels=(1,))}
    assert result["MARCOS_SINGLE_BOARD_RX"] == 1


def test_mri_only_on_a_follower_keeps_board0_as_trigger():
    result = layout(dict(rx0="emi", tx0=True), dict(ip="169.254.93.61", rx0="mri", rx1="off"))
    assert result["MARCOS_BOARDS"] == {
        0: dict(ip=SCANNER, port=11111, rx_channels=()),
        1: dict(ip="169.254.93.61", port=11111, rx_channels=(0,)),
    }
    assert result["MARCOS_SINGLE_BOARD_RX"] is None


def test_emi_only_follower_is_not_part_of_fid():
    result = layout(BOARD0, dict(ip="169.254.93.61", rx0="emi", rx1="emi"))
    assert result["MARCOS_BOARDS"] is None
    assert 1 in result["MARCOS_BOARD_ADDRESSES"]


def test_follower_without_address_is_left_out():
    result = layout(BOARD0, dict(ip="", rx0="mri"))
    assert result["MARCOS_BOARDS"] is None and 1 not in result["MARCOS_BOARD_ADDRESSES"]
    assert result["MARCOS_BOARD_PROBLEMS"] == ["Board 1 has no address, so it is left out."]


def test_no_mri_receiver_is_reported():
    result = layout(dict(rx0="off", tx0=True))
    assert result["MARCOS_BOARDS"] is None and result["MARCOS_SINGLE_BOARD_RX"] is None
    assert "No receiver is marked MRI" in result["MARCOS_BOARD_PROBLEMS"][0]


def test_missing_scanner_ip_is_reported():
    result = cfg.board_layout(MarcosParameters().boards, "", 11111)
    assert result["MARCOS_BOARDS"] is None and result["MARCOS_BOARD_ADDRESSES"] == {}
    assert result["MARCOS_BOARD_PROBLEMS"] == ["Board 0 needs a Scanner IP."]


# --- EMI follows the receivers marked EMI ------------------------------------

ADDRESSES = {0: dict(ip=SCANNER, port=11111), 1: dict(ip="169.254.93.61", port=11111)}


def test_emi_listens_on_receivers_marked_emi():
    roles = {(0, 0): "mri", (0, 1): "emi", (1, 0): "emi"}
    assert emi.emi_channels(ADDRESSES, roles=roles) == [(0, 1, "emi"), (1, 0, "emi")]
    assert emi.emi_channels(ADDRESSES, include_reference=True, roles=roles)[0] == (0, 0, "mri_reference")
    # a board without an address is skipped
    assert emi.emi_channels({0: ADDRESSES[0]}, roles=roles) == [(0, 1, "emi")]


def test_emi_status_without_emi_receivers():
    roles = {(0, 0): "mri"}
    hardware = emi.status(simulation=False, boards=ADDRESSES, busy=False, roles=roles)
    assert hardware["available"] is False and "marked EMI" in hardware["reason"]
    # the simulator still shows the standard layout
    simulated = emi.status(simulation=True, boards=ADDRESSES, busy=False, roles=roles)
    assert simulated["available"] is True and len(simulated["channels"]) == 4
    with pytest.raises(emi.EmiUnavailable, match="marked EMI"):
        emi.measure(simulation=False, boards=ADDRESSES, center_frequency_hz=2e6, roles=roles, acquire=lambda *a: None)


def test_emi_burst_always_includes_board0():
    from services.api import emi_acq

    settings = emi_acq._board_settings(ADDRESSES, [(1, 0, "emi"), (1, 1, "emi")])
    assert settings == {0: dict(ADDRESSES[0], rx_channels=()), 1: dict(ADDRESSES[1], rx_channels=(0, 1))}
    with pytest.raises(emi.EmiUnavailable, match="Scanner IP"):
        emi_acq._board_settings({1: ADDRESSES[1]}, [(1, 0, "emi")])


# --- scans use the chosen TX outputs and receiver ----------------------------

import external.seq.adjustments_acq.scripts as scripts  # noqa: E402

END = 1000.0


class FakePsi:
    """Stands in for seq2flocra: one RF pulse, one ADC window on the requested receivers."""

    last_rx_channels = None

    def __init__(self, rx_channels=(0,), **_):
        FakePsi.last_rx_channels = tuple(rx_channels)
        self._rx_t, self._grad_t = 3.125, 10.0
        adc = (np.array([0.0, 200.0, 200.0 + 64 * 3.125, END]), np.array([0.0, 1.0, 0.0, 0.0]))
        self._flo_dict = {
            "tx_gate": (np.array([0.0, 120.0, END]), np.array([1.0, 0.0, 0.0])),
            "tx0": (np.array([0.0, 50.0, 100.0, END]), np.array([0, 0.5, 0, 0], dtype=complex)),
            "grad_vx": (np.array([0.0, END]), np.array([0.0, 0.0])),
            "grad_vy": (np.array([0.0, END]), np.array([0.0, 0.0])),
            "grad_vz": (np.array([0.0, END]), np.array([0.0, 0.0])),
        }
        for ch in (0, 1):
            self._flo_dict[f"rx{ch}_en"] = (adc[0].copy(), adc[1].copy() if ch in rx_channels else np.zeros(4))

    def load_seqfile(self, _):
        pass

    def block_events_to_amps_times(self):
        pass


class FakeExperiment:
    """Records what one board was told to do and returns a marker on each receiver."""

    made = []

    def __init__(self, **kwargs):
        self.kwargs, self.flodict, self._initial_wait = kwargs, {}, 17.0
        FakeExperiment.made.append(self)

    def add_flodict(self, flodict, append=True):
        self.flodict.update(flodict)

    def run(self, *_):
        return {"rx0": np.full(64, 0.0 + 0j), "rx1": np.full(64, 1.0 + 0j)}, {}

    def __del__(self):
        pass


class FakeMulti:
    def __init__(self, boards):
        self.exps = {board_id: FakeExperiment(**options) for board_id, options in boards.items()}

    def add_flodict(self, flodict, append=True):
        for board_id, events in flodict.items():
            self.exps[board_id].add_flodict(events)

    def run(self, *_):
        out = {board_id: exp.run()[0] for board_id, exp in self.exps.items()}
        return out, {}


@pytest.fixture
def fake_boards(monkeypatch):
    FakeExperiment.made = []
    monkeypatch.setattr(cfg, "update", lambda: None)
    monkeypatch.setattr(scripts, "seq2flocra", FakePsi)
    monkeypatch.setattr(scripts.ex, "Experiment", FakeExperiment)
    monkeypatch.setattr(scripts, "load_multi_experiment", lambda boards: FakeMulti)
    monkeypatch.setattr(scripts, "_close_board_connections", lambda expt: None)
    return monkeypatch


def run(boards=None):
    return scripts.run_pulseq("x.seq", rf_center=2.0, system=object(), hardware_simulation=False, boards=boards)


def test_single_board_sequence_uses_the_chosen_receiver_tx_and_cic(fake_boards):
    fake_boards.setattr(cfg, "MARCOS_SINGLE_BOARD_RX", 1)
    fake_boards.setattr(cfg, "MARCOS_TX_CHANNELS", (1,))
    fake_boards.setattr(cfg, "MARCOS_BOARD_ADDRESSES", {0: dict(ip=SCANNER, port=11111, set_cic_shift=True)})
    data, _ = run()
    assert np.all(data == 1.0)  # RX1's marker
    assert FakePsi.last_rx_channels == (1,)
    board0 = FakeExperiment.made[-1]
    assert "tx1" in board0.flodict and "tx0" not in board0.flodict
    assert board0.kwargs["set_cic_shift"] is True


def test_single_board_default_is_unchanged(fake_boards):
    fake_boards.setattr(cfg, "MARCOS_SINGLE_BOARD_RX", 0)
    fake_boards.setattr(cfg, "MARCOS_TX_CHANNELS", (0,))
    fake_boards.setattr(cfg, "MARCOS_BOARD_ADDRESSES", {0: dict(ip=SCANNER, port=11111)})
    data, _ = run()
    assert np.all(data == 0.0)
    board0 = FakeExperiment.made[-1]
    assert "tx0" in board0.flodict and "tx1" not in board0.flodict
    assert board0.kwargs["set_cic_shift"] is False


def test_single_board_sequence_refuses_without_an_mri_receiver(fake_boards):
    fake_boards.setattr(cfg, "MARCOS_SINGLE_BOARD_RX", None)
    with pytest.raises(ValueError, match="no receiver marked MRI"):
        run()


def test_multi_board_pulse_plays_on_both_tx_outputs_of_board0_only(fake_boards):
    fake_boards.setattr(cfg, "MARCOS_TX_CHANNELS", (0, 1))
    fake_boards.setattr(cfg, "MARCOS_ALIGN_FOLLOWERS", False)
    fake_boards.setattr(cfg, "MARCOS_RUN_TIMEOUT_S", 5)
    boards = {0: dict(ip=SCANNER, port=11111, rx_channels=(0,)),
              1: dict(ip="169.254.93.61", port=11111, rx_channels=(1,))}
    rxd, _ = run(boards)
    assert set(rxd) == {0, 1} and set(rxd[0]) == {"rx0"} and set(rxd[1]) == {"rx1"}
    board0, board1 = FakeExperiment.made[-2:]
    assert {"tx0", "tx1"} <= set(board0.flodict)
    np.testing.assert_array_equal(board0.flodict["tx0"][1], board0.flodict["tx1"][1])
    assert not {"tx0", "tx1"} & set(board1.flodict)
