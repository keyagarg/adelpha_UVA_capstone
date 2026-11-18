"""Record one EMI noise burst on the MaRCoS boards.

Kept apart from ``emi.py`` because it pulls in pypulseq and the MaRCoS client,
which the simulator and its tests do not need.

The burst reuses the Noise Scan sequence (a spin echo with both flip angles at
zero, so nothing is transmitted) and the multi-board path of ``run_pulseq``,
with each board's ``rx_channels`` replaced by the EMI channel map.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

import common.logger as logger
from services.api.emi import EmiUnavailable

log = logger.get_logger()

# Timing of the Noise Scan sequence, which is known to run on this hardware.
_MIN_TE_MS = 70
_MIN_TR_MS = 250


def _board_settings(boards: dict, channels: List[Tuple[int, int, str]]) -> dict:
    """``MARCOS_BOARDS`` with each board's receivers replaced by the EMI ones."""
    wanted: Dict[int, List[int]] = {}
    for board, channel, _role in channels:
        wanted.setdefault(board, []).append(channel)
    return {board: {**boards[board], "rx_channels": tuple(sorted(chs))} for board, chs in sorted(wanted.items())}


def _ensure_servers(emi_boards: dict) -> None:
    """Same preflight a scan gets: every board must answer as a MaRCoS server."""
    import external.seq.adjustments_acq.config as cfg
    from sequences.common.util import reading_json_parameter
    from services.ui.control import probe_scanner
    from services.ui.marcos_boot import ensure_marcos_server, fpga_device

    for ip, port in cfg.marcos_board_endpoints(emi_boards).values():
        if probe_scanner(ip, port=port).get("method") == "tcp":
            continue
        clock = reading_json_parameter().marcos_parameters.fpga_clock_frequency_MHz
        boot = ensure_marcos_server(ip, port=port, device=fpga_device(clock))
        if not boot["ok"]:
            raise EmiUnavailable(boot["detail"] or f"MaRCoS server is not running at {ip}:{port}")


def acquire(
    boards: dict,
    channels: List[Tuple[int, int, str]],
    n_samples: int,
    adc_duration_us: float,
    averages: int,
) -> Tuple[Dict[Tuple[int, int], np.ndarray], float, float]:
    """Return ``(samples, sample_period_s, center_frequency_hz)`` for one burst.

    ``samples`` maps ``(board, channel)`` to complex bursts shaped
    ``(averages, n_samples)``.
    """
    import external.seq.adjustments_acq.config as cfg
    from external.marcos_client.local_config import apply_scanner_settings
    from external.seq.adjustments_acq.scripts import run_pulseq
    from sequences.common import make_rf_se

    cfg.update()
    apply_scanner_settings()
    emi_boards = _board_settings(boards, channels)
    _ensure_servers(emi_boards)

    adc_ms = adc_duration_us / 1000.0
    te_ms = max(_MIN_TE_MS, math.ceil(adc_ms) + 10)
    tr_ms = max(_MIN_TR_MS, te_ms + math.ceil(adc_ms) + 50)

    with tempfile.TemporaryDirectory(prefix="adelpha-emi-") as folder:
        seq_file = str(Path(folder) / "emi_noise.seq")
        written = make_rf_se.pypulseq_rfse(
            inputs={
                "TE": te_ms,
                "TR": tr_ms,
                "NSA": averages,
                "ADC_samples": n_samples,
                "ADC_duration": adc_duration_us,
                "FA1": 0,
                "FA2": 0,
            },
            check_timing=True,
            output_file=seq_file,
        )
        if not written:
            raise EmiUnavailable("Could not write the EMI noise sequence")
        log.info("EMI burst on boards %s", {board: s["rx_channels"] for board, s in emi_boards.items()})
        rxd, rx_t_us = run_pulseq(
            seq_file=seq_file,
            rf_center=cfg.LARMOR_FREQ,
            tx_t=1,
            grad_t=10,
            tx_warmup=100,
            shim_x=0,
            shim_y=0,
            shim_z=0,
            grad_cal=False,
            save_np=False,
            save_mat=False,
            save_msgs=False,
            gui_test=False,
            case_path=folder,
            hardware_simulation=False,
            boards=emi_boards,
        )

    samples: Dict[Tuple[int, int], np.ndarray] = {}
    for board, channel, _role in channels:
        data = np.asarray(rxd[board][f"rx{channel}"]).ravel()
        per_burst = data.size // averages
        if per_burst == 0:
            raise EmiUnavailable(f"Board {board} RX{channel} returned no samples")
        if data.size != averages * n_samples:
            log.warning(
                "Board %s RX%s returned %s samples; expected %s", board, channel, data.size, averages * n_samples
            )
        samples[(board, channel)] = data[: per_burst * averages].reshape(averages, per_burst)

    sizes = {bursts.shape for bursts in samples.values()}
    if len(sizes) != 1:
        raise EmiUnavailable(f"Receive channels returned different sample counts: {sorted(sizes)}")
    return samples, float(rx_t_us) * 1e-6, float(cfg.LARMOR_FREQ) * 1e6
