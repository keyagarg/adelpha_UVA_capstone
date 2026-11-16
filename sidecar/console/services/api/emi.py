"""EMI visualizer backend: channel map, simulated noise bursts, spectra.

One *capture* is one noise burst recorded on every EMI receive channel at the
same time. Board 0 RX0 carries the MRI signal, so the EMI tool listens on the
other receivers and only adds board 0 RX0 when the caller asks for it as a
reference trace.

The generator below produces time samples that go through the same FFT and 
payload code a real MaRCoS burst will use, so the measured path only has to 
replace ``simulate_samples``.

This module depends on numpy alone. Reading the scanner configuration and the
HTTP layer stay in ``app.py``.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

# board id -> receive channels the EMI tool listens on.
EMI_RX_CHANNELS: Dict[int, Tuple[int, ...]] = {0: (1,), 1: (0, 1)}
# Board 0 RX0 is generally the MRI receive channel; shown only as an optional reference.
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


def emi_channels(boards: Optional[dict], include_reference: bool = False) -> List[Tuple[int, int, str]]:
    """List ``(board, channel, role)`` for every channel one capture records.

    ``boards`` is ``MARCOS_BOARDS`` from the acquisition config: a dict keyed
    by board id, or ``None`` when multi-board is not configured. With ``None``
    the full two-board layout is returned so the simulator can stand in for
    hardware that is not connected.
    """
    board_ids = sorted(EMI_RX_CHANNELS) if not boards else sorted(b for b in boards if b in EMI_RX_CHANNELS)
    channels: List[Tuple[int, int, str]] = []
    if include_reference and MRI_REFERENCE_CHANNEL[0] in board_ids:
        channels.append((MRI_REFERENCE_CHANNEL[0], MRI_REFERENCE_CHANNEL[1], ROLE_MRI_REFERENCE))
    for board in board_ids:
        for channel in EMI_RX_CHANNELS[board]:
            channels.append((board, channel, ROLE_EMI))
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

_lock = threading.Lock()
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
) -> dict:
    """Record one capture and return its payload.

    Only one capture runs at a time. Real acquisition arrives in stage 5; until
    then this raises ``EmiUnavailable`` when hardware simulation is off.
    """
    global _step, _latest
    if not simulation:
        raise EmiUnavailable(
            "Real EMI acquisition is not available yet. Turn on Hardware Simulation to use simulated data."
        )
    channels = emi_channels(boards, include_reference)
    if not channels:
        raise EmiUnavailable("No EMI receive channels are configured")
    sample_period_s = adc_duration_us * 1e-6 / n_samples
    roles = {(board, channel): role for board, channel, role in channels}

    with _lock:
        samples = simulate_samples(channels, n_samples, sample_period_s, averages, step=_step, rng=rng)
        _step += 1
        capture = build_capture(
            samples,
            roles,
            source="simulated",
            center_frequency_hz=center_frequency_hz,
            sample_period_s=sample_period_s,
        )
        _latest = {"capture": capture, "samples": samples}
    return capture


def status(*, simulation: bool, boards: Optional[dict], busy: bool) -> dict:
    """Whether the EMI tool can measure right now, and on which channels."""
    channels = [
        {"board": board, "channel": f"rx{channel}", "label": channel_label(board, channel), "role": role}
        for board, channel, role in emi_channels(boards, include_reference=True)
    ]
    reason = ""
    if not simulation:
        reason = "Real EMI acquisition is not available yet. Turn on Hardware Simulation to use simulated data."
    elif busy:
        reason = "A scan is running."
    return {
        "available": simulation and not busy,
        "busy": busy,
        "simulation": simulation,
        "channels": channels,
        "reason": reason,
    }
