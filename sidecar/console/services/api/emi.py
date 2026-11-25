"""EMI visualizer backend: channel map, simulated noise bursts, spectra.

One *capture* is one noise burst recorded on every EMI receive channel at the
same time. Board 0 RX0 carries the MRI signal, so the EMI tool listens on the
other receivers and only adds board 0 RX0 when the caller asks for it as a
reference trace.

Simulated and measured bursts share one path: both produce time samples that
go through the same FFT and payload code. ``simulate_samples`` generates them
here; real bursts come from ``emi_acq.acquire``, which ``app.py`` passes in.

This module depends on numpy alone. Reading the scanner configuration, the
MaRCoS acquisition, and the HTTP layer live elsewhere.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

# board id -> receive channels the EMI tool listens on.
EMI_RX_CHANNELS: Dict[int, Tuple[int, ...]] = {0: (1,), 1: (0, 1)}
# Board 0 RX0 is the MRI receive channel; shown only as an optional reference.
MRI_REFERENCE_CHANNEL: Tuple[int, int] = (0, 0)

ROLE_EMI = "emi"
ROLE_MRI_REFERENCE = "mri_reference"

DEFAULT_N_SAMPLES = 4096
DEFAULT_ADC_DURATION_US = 6400.0
DEFAULT_AVERAGES = 1

UNITS = "a.u."

# (board, channel) -> role
ChannelKey = Tuple[int, int]


class EmiUnavailable(RuntimeError):
    """The EMI tool cannot measure right now; the message says why."""


class EmiBusy(EmiUnavailable):
    """The boards are in use by a scan or by another EMI capture."""


NO_BOARDS_REASON = (
    "No MaRCoS boards are configured (MARCOS_BOARDS is empty). Add them in "
    "Settings > Imaging Console > MaRCoS boards, or turn on Hardware Simulation."
)
NO_EMI_CHANNEL_REASON = (
    "No receiver is marked EMI. Mark one in Settings > Imaging Console > MaRCoS boards, "
    "or turn on Hardware Simulation."
)

# Held for the length of one capture, and by the acquisition pipeline for the
# length of one scan, so a burst and a scan never drive the boards together.
hardware_lock = threading.Lock()

# (boards, channels, n_samples, adc_duration_us, averages)
#   -> (samples, sample_period_s, center_frequency_hz)
AcquireFn = Callable[
    [dict, List[Tuple[int, int, str]], int, float, int],
    Tuple[Dict["ChannelKey", np.ndarray], float, float],
]


def emi_channels(
    boards: Optional[dict],
    include_reference: bool = False,
    roles: Optional[Dict[ChannelKey, str]] = None,
) -> List[Tuple[int, int, str]]:
    """List ``(board, channel, role)`` for every channel one capture records.

    ``boards`` is ``MARCOS_BOARDS`` from the acquisition config: a dict keyed
    by board id, or ``None`` when multi-board is not configured. With ``None``
    the full two-board layout is returned so the simulator can stand in for
    hardware that is not connected.

    ``roles`` maps ``(board, channel)`` to "mri" or "emi", as set in Settings >
    Imaging Console > MaRCoS boards. When given, the EMI channels are the ones
    marked "emi" and the reference traces the ones marked "mri", on boards listed
    in ``boards``. Without it the fixed ``EMI_RX_CHANNELS`` map is used.
    """
    if roles is not None:
        known = (lambda board: board in boards) if boards else (lambda board: True)
        marked = [(board, channel, role) for (board, channel), role in sorted(roles.items()) if known(board)]
        references = [(b, c, ROLE_MRI_REFERENCE) for b, c, role in marked if role == "mri"]
        return (references if include_reference else []) + [(b, c, ROLE_EMI) for b, c, role in marked if role == "emi"]
    board_ids = sorted(EMI_RX_CHANNELS) if not boards else sorted(b for b in boards if b in EMI_RX_CHANNELS)
    channels: List[Tuple[int, int, str]] = []
    if include_reference and MRI_REFERENCE_CHANNEL[0] in board_ids:
        channels.append((MRI_REFERENCE_CHANNEL[0], MRI_REFERENCE_CHANNEL[1], ROLE_MRI_REFERENCE))
    for board in board_ids:
        for channel in EMI_RX_CHANNELS[board]:
            channels.append((board, channel, ROLE_EMI))
    return channels


def _capture_channels(
    simulation: bool,
    boards: Optional[dict],
    roles: Optional[Dict[ChannelKey, str]],
    include_reference: bool,
) -> List[Tuple[int, int, str]]:
    """Channels for one capture. In simulation with no receiver marked EMI, the simulator
    shows the standard two-board layout instead."""
    channels = emi_channels(boards, include_reference, roles)
    if simulation and roles is not None and not any(role == ROLE_EMI for _, _, role in channels):
        channels = emi_channels(None, include_reference)
    return channels


def channel_label(board: int, channel: int) -> str:
    return f"Board {board} RX{channel}"


# --- Simulation -------------------------------------------------------------

_NOISE_SIGMA = 0.01
_TONE_AMPLITUDE = 0.004
# Tone positions as a fraction of the bandwidth, measured from the centre.
_TONE_OFFSETS = (-0.28, 0.09, 0.34)
# The middle tone wanders by this fraction of the bandwidth between captures.
_DRIFT_FRACTION = 0.025
_DRIFT_RATE = 0.15
# How strongly each channel picks up each tone. Different on every channel so
# the traces are easy to tell apart on the plot.
_TONE_GAINS: Dict[ChannelKey, Tuple[float, float, float]] = {
    (0, 1): (1.0, 0.6, 0.2),
    (1, 0): (0.5, 1.0, 0.4),
    (1, 1): (0.2, 0.4, 1.0),
    # The MRI coil sits inside the shield, so it sees much weaker interference.
    (0, 0): (0.05, 0.08, 0.03),
}


def simulate_samples(
    channels: List[Tuple[int, int, str]],
    n_samples: int,
    sample_period_s: float,
    averages: int = 1,
    step: int = 0,
    rng: Optional[np.random.Generator] = None,
) -> Dict[ChannelKey, np.ndarray]:
    """Generate complex baseband noise bursts, shaped ``(averages, n_samples)``.

    Each channel gets a white-noise floor plus three narrow interference
    tones. ``step`` counts captures; the middle tone drifts with it so repeated
    captures visibly change.
    """
    rng = rng if rng is not None else np.random.default_rng()
    bandwidth_hz = 1.0 / sample_period_s
    t = np.arange(n_samples) * sample_period_s
    offsets_hz = [fraction * bandwidth_hz for fraction in _TONE_OFFSETS]
    offsets_hz[1] += _DRIFT_FRACTION * bandwidth_hz * np.sin(_DRIFT_RATE * step)

    samples: Dict[ChannelKey, np.ndarray] = {}
    for board, channel, _role in channels:
        gains = _TONE_GAINS.get((board, channel), (0.3, 0.3, 0.3))
        bursts = np.empty((averages, n_samples), dtype=np.complex128)
        for index in range(averages):
            noise = rng.normal(0.0, _NOISE_SIGMA / np.sqrt(2.0), (2, n_samples))
            burst = noise[0] + 1j * noise[1]
            for offset_hz, gain in zip(offsets_hz, gains):
                phase = rng.uniform(0.0, 2.0 * np.pi)
                burst = burst + _TONE_AMPLITUDE * gain * np.exp(1j * (2.0 * np.pi * offset_hz * t + phase))
            bursts[index] = burst
        samples[(board, channel)] = bursts
    return samples


# --- Spectrum and payload ---------------------------------------------------


def frequency_axis_hz(n_samples: int, sample_period_s: float, center_frequency_hz: float) -> np.ndarray:
    """Absolute frequency of each spectrum bin, lowest first."""
    return center_frequency_hz + np.fft.fftshift(np.fft.fftfreq(n_samples, d=sample_period_s))


def magnitude_spectrum(bursts: np.ndarray) -> np.ndarray:
    """Magnitude spectrum of ``(averages, n_samples)`` complex bursts.

    Bursts are averaged in power, not as complex samples: averaging complex
    noise would cancel the very signal this tool is here to show.
    """
    bursts = np.atleast_2d(bursts)
    spectra = np.fft.fftshift(np.fft.fft(bursts, axis=1), axes=1) / bursts.shape[1]
    return np.sqrt(np.mean(np.abs(spectra) ** 2, axis=0))


def _compact(values: np.ndarray) -> List[float]:
    """Six significant figures: plenty for a plot, a third of the JSON size."""
    return [float(f"{value:.6g}") for value in values]


def build_capture(
    samples: Dict[ChannelKey, np.ndarray],
    roles: Dict[ChannelKey, str],
    *,
    source: str,
    center_frequency_hz: float,
    sample_period_s: float,
    now: Optional[datetime] = None,
) -> dict:
    """Turn raw bursts into the JSON-ready ``EmiCapture`` payload."""
    if not samples:
        raise EmiUnavailable("No receive channels were recorded")
    now = now or datetime.now(timezone.utc)
    first = np.atleast_2d(next(iter(samples.values())))
    averages, n_samples = first.shape
    frequency = frequency_axis_hz(n_samples, sample_period_s, center_frequency_hz)

    traces = []
    for (board, channel), bursts in samples.items():
        bursts = np.atleast_2d(bursts)
        if bursts.shape != (averages, n_samples):
            raise ValueError("Every channel must record the same number of samples")
        magnitude = magnitude_spectrum(bursts)
        peak = int(np.argmax(magnitude))
        traces.append(
            {
                "board": board,
                "channel": f"rx{channel}",
                "label": channel_label(board, channel),
                "role": roles.get((board, channel), ROLE_EMI),
                "magnitude": _compact(magnitude),
                "time_magnitude": _compact(np.abs(bursts[-1])),
                "rms": float(np.sqrt(np.mean(np.abs(bursts) ** 2))),
                "peak_frequency_hz": float(frequency[peak]),
                "peak_magnitude": float(magnitude[peak]),
            }
        )

    return {
        "capture_id": "emi-" + now.strftime("%Y%m%d-%H%M%S-") + f"{now.microsecond // 1000:03d}",
        "timestamp": now.isoformat(),
        "source": source,
        "center_frequency_hz": float(center_frequency_hz),
        "sample_period_s": float(sample_period_s),
        "bandwidth_hz": float(1.0 / sample_period_s),
        "n_samples": int(n_samples),
        "averages": int(averages),
        "units": UNITS,
        "frequency_hz": np.round(frequency, 1).tolist(),
        "traces": traces,
    }


# --- Capture state ----------------------------------------------------------

_step = 0
# Raw samples of the most recent capture, kept so a later stage can save them.
_latest: Optional[dict] = None


def latest_capture() -> Optional[dict]:
    """``{"capture": payload, "samples": raw bursts}`` of the last capture."""
    return _latest


def measure(
    *,
    simulation: bool,
    boards: Optional[dict],
    center_frequency_hz: float,
    n_samples: int = DEFAULT_N_SAMPLES,
    adc_duration_us: float = DEFAULT_ADC_DURATION_US,
    averages: int = DEFAULT_AVERAGES,
    include_reference: bool = False,
    rng: Optional[np.random.Generator] = None,
    acquire: Optional[AcquireFn] = None,
    roles: Optional[Dict[ChannelKey, str]] = None,
) -> dict:
    """Record one capture and return its payload.

    With ``simulation`` the samples are generated here. Otherwise ``acquire``
    records them on the boards. Only one capture runs at a time, and never
    during a scan: ``EmiBusy`` is raised instead of waiting.
    """
    global _step, _latest
    if not simulation and not boards:
        raise EmiUnavailable(NO_BOARDS_REASON)
    if not simulation and acquire is None:
        raise EmiUnavailable("No acquisition backend is available for measured EMI captures")
    channels = _capture_channels(simulation, boards, roles, include_reference)
    if not any(role == ROLE_EMI for _, _, role in channels):
        raise EmiUnavailable(NO_EMI_CHANNEL_REASON if roles is not None else "No EMI receive channels are configured")
    roles = {(board, channel): role for board, channel, role in channels}

    if not hardware_lock.acquire(blocking=False):
        raise EmiBusy("The boards are busy with a scan or another EMI capture.")
    try:
        if simulation:
            sample_period_s = adc_duration_us * 1e-6 / n_samples
            samples = simulate_samples(channels, n_samples, sample_period_s, averages, step=_step, rng=rng)
        else:
            try:
                samples, sample_period_s, center_frequency_hz = acquire(
                    boards, channels, n_samples, adc_duration_us, averages
                )
            except EmiUnavailable:
                raise
            except Exception as exc:
                raise EmiUnavailable(f"EMI acquisition failed: {exc}") from exc
        _step += 1
        capture = build_capture(
            samples,
            roles,
            source="simulated" if simulation else "measured",
            center_frequency_hz=center_frequency_hz,
            sample_period_s=sample_period_s,
        )
        _latest = {"capture": capture, "samples": samples}
    finally:
        hardware_lock.release()
    return capture


def save_latest(capture_id: str, emi_dir) -> dict:
    """Write the latest capture under ``emi_dir/<capture_id>/``.

    Raw complex samples go to ``rawdata/board<b>_rx<c>.npy``, one file per
    channel, shaped ``(averages, n_samples)``. ``capture.json`` holds the
    metadata and per-channel summary, without the plotted arrays.
    """
    latest = _latest
    if latest is None or latest["capture"]["capture_id"] != capture_id:
        raise EmiUnavailable("That capture is no longer held in memory. Measure again, then save.")
    capture = latest["capture"]
    folder = Path(emi_dir) / capture_id
    raw_dir = folder / "rawdata"
    raw_dir.mkdir(parents=True, exist_ok=True)

    files = []
    traces = []
    for trace in capture["traces"]:
        board, channel = trace["board"], trace["channel"]
        relative = f"rawdata/board{board}_{channel}.npy"
        np.save(folder / relative, latest["samples"][(board, int(channel[2:]))])
        files.append(relative)
        summary = {key: value for key, value in trace.items() if key not in ("magnitude", "time_magnitude")}
        summary["file"] = relative
        traces.append(summary)

    metadata = {key: value for key, value in capture.items() if key not in ("frequency_hz", "traces")}
    metadata["sample_layout"] = "complex, shape (averages, n_samples)"
    metadata["traces"] = traces
    (folder / "capture.json").write_text(json.dumps(metadata, indent=2))
    files.append("capture.json")
    return {"capture_id": capture_id, "folder": str(folder), "files": files}


def status(
    *,
    simulation: bool,
    boards: Optional[dict],
    busy: bool,
    roles: Optional[Dict[ChannelKey, str]] = None,
) -> dict:
    """Whether the EMI tool can measure right now, and on which channels."""
    listed = _capture_channels(simulation, boards, roles, include_reference=True)
    channels = [
        {"board": board, "channel": f"rx{channel}", "label": channel_label(board, channel), "role": role}
        for board, channel, role in listed
    ]
    reason = ""
    if not simulation and not boards:
        reason = NO_BOARDS_REASON
    elif not simulation and not any(role == ROLE_EMI for _, _, role in listed):
        reason = NO_EMI_CHANNEL_REASON
    elif busy:
        reason = "A scan is running."
    return {
        "available": not reason,
        "busy": busy,
        "simulation": simulation,
        "channels": channels,
        "reason": reason,
    }
