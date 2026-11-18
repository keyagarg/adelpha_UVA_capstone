import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.api import emi

CENTER_HZ = 2.04e6
TWO_BOARDS = {0: dict(ip="10.0.0.1"), 1: dict(ip="10.0.0.2")}


def test_channel_map_skips_the_mri_channel():
    assert emi.emi_channels(TWO_BOARDS) == [(0, 1, "emi"), (1, 0, "emi"), (1, 1, "emi")]


def test_reference_trace_is_opt_in_and_listed_first():
    channels = emi.emi_channels(TWO_BOARDS, include_reference=True)
    assert channels[0] == (0, 0, "mri_reference")
    assert len(channels) == 4


def test_single_board_gives_one_emi_trace():
    assert emi.emi_channels({0: dict(ip="10.0.0.1")}) == [(0, 1, "emi")]


def test_unconfigured_boards_fall_back_to_the_full_layout():
    assert len(emi.emi_channels(None)) == 3


def test_frequency_axis_is_centred_and_ascending():
    axis = emi.frequency_axis_hz(4096, 6400e-6 / 4096, CENTER_HZ)
    assert len(axis) == 4096
    assert np.all(np.diff(axis) > 0)
    assert axis[2048] == pytest.approx(CENTER_HZ)
    assert axis[-1] - axis[0] == pytest.approx(640_000, rel=1e-3)


def test_spectrum_finds_a_known_tone():
    n, period = 1024, 1e-6
    t = np.arange(n) * period
    tone = 0.5 * np.exp(2j * np.pi * 125_000 * t)
    magnitude = emi.magnitude_spectrum(tone)
    axis = emi.frequency_axis_hz(n, period, 0.0)
    assert axis[int(np.argmax(magnitude))] == pytest.approx(125_000)
    assert magnitude.max() == pytest.approx(0.5)


def test_measure_returns_three_simulated_traces():
    capture = emi.measure(
        simulation=True, boards=TWO_BOARDS, center_frequency_hz=CENTER_HZ, rng=np.random.default_rng(1)
    )
    assert capture["source"] == "simulated"
    assert capture["units"] == "a.u."
    assert capture["n_samples"] == 4096
    assert capture["bandwidth_hz"] == pytest.approx(640_000)
    assert [(t["board"], t["channel"]) for t in capture["traces"]] == [(0, "rx1"), (1, "rx0"), (1, "rx1")]
    for trace in capture["traces"]:
        assert len(trace["magnitude"]) == len(capture["frequency_hz"]) == 4096
        assert len(trace["time_magnitude"]) == 4096
        assert trace["rms"] > 0
        # Interference tones must stand well clear of the noise floor.
        assert trace["peak_magnitude"] > 8 * float(np.median(trace["magnitude"]))


def test_measure_keeps_raw_samples_for_saving():
    capture = emi.measure(
        simulation=True, boards=TWO_BOARDS, center_frequency_hz=CENTER_HZ, averages=3, include_reference=True
    )
    latest = emi.latest_capture()
    assert latest["capture"]["capture_id"] == capture["capture_id"]
    assert set(latest["samples"]) == {(0, 0), (0, 1), (1, 0), (1, 1)}
    assert latest["samples"][(0, 1)].shape == (3, 4096)
    assert capture["averages"] == 3
    assert capture["traces"][0]["role"] == "mri_reference"


def test_save_writes_raw_samples_and_metadata(tmp_path):
    import json

    capture = emi.measure(simulation=True, boards=TWO_BOARDS, center_frequency_hz=CENTER_HZ, averages=2)
    saved = emi.save_latest(capture["capture_id"], tmp_path)
    folder = tmp_path / capture["capture_id"]
    assert saved["folder"] == str(folder)
    assert saved["files"] == [
        "rawdata/board0_rx1.npy",
        "rawdata/board1_rx0.npy",
        "rawdata/board1_rx1.npy",
        "capture.json",
    ]
    raw = np.load(folder / "rawdata" / "board1_rx0.npy")
    assert raw.shape == (2, 4096) and np.iscomplexobj(raw)
    assert np.array_equal(raw, emi.latest_capture()["samples"][(1, 0)])
    metadata = json.loads((folder / "capture.json").read_text())
    assert metadata["source"] == "simulated" and metadata["averages"] == 2
    assert "frequency_hz" not in metadata
    assert metadata["traces"][0]["file"] == "rawdata/board0_rx1.npy"
    assert "magnitude" not in metadata["traces"][0]


def test_save_refuses_a_capture_that_is_no_longer_held(tmp_path):
    emi.measure(simulation=True, boards=TWO_BOARDS, center_frequency_hz=CENTER_HZ)
    with pytest.raises(emi.EmiUnavailable):
        emi.save_latest("emi-something-else", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_drifting_tone_moves_between_captures():
    channels = emi.emi_channels(TWO_BOARDS)
    period = 6400e-6 / 4096
    axis = emi.frequency_axis_hz(4096, period, 0.0)
    peaks = []
    for step in (0, 10):
        samples = emi.simulate_samples(channels, 4096, period, step=step, rng=np.random.default_rng(2))
        # Board 1 RX0 is dominated by the drifting middle tone.
        peaks.append(axis[int(np.argmax(emi.magnitude_spectrum(samples[(1, 0)])))])
    assert abs(peaks[1] - peaks[0]) > 5_000


def test_measure_refuses_without_simulation():
    with pytest.raises(emi.EmiUnavailable):
        emi.measure(simulation=False, boards=TWO_BOARDS, center_frequency_hz=CENTER_HZ)


def test_status_reports_busy_and_channels():
    idle = emi.status(simulation=True, boards=TWO_BOARDS, busy=False)
    assert idle["available"] is True and idle["reason"] == ""
    assert len(idle["channels"]) == 4
    busy = emi.status(simulation=True, boards=TWO_BOARDS, busy=True)
    assert busy["available"] is False and "scan" in busy["reason"].lower()
    hardware = emi.status(simulation=False, boards=TWO_BOARDS, busy=False)
    assert hardware["available"] is False and hardware["reason"]
