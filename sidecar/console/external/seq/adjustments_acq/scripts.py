#!/usr/bin/env python3
#
# Run a pulseq file
# Code by Lincoln Craven-Brightman
#

import sys
import os
import socket
import threading
from pathlib import Path
from importlib import import_module
import numpy as np
import matplotlib.pyplot as plt
import scipy.io as sio
import scipy.signal as sig
from operator import itemgetter

import external.seq.adjustments_acq.config as cfg  # pylint: disable=import-error
import external.marcos_client.experiment as ex  # pylint: disable=import-error

from external.flocra_pulseq.interpreter_pp import seq2flocra, normalize_rx_channels


import common.helper as helper
from common.constants import *
import common.logger as logger

log = logger.get_logger()

from common.ipc import Communicator


ipc_comm = Communicator(Communicator.ACQ)


def load_multi_experiment(boards):
    """Load the teammate's bare imports against the configured console modules."""
    names = ("local_config", "marmachine", "grad_board", "server_comms", "marcompile",
             "experiment", "experimentController", "experimentFollower", "experimentExternal")
    previous = {name: sys.modules.get(name) for name in names}
    try:
        for name in names:
            sys.modules[name] = import_module(f"external.marcos_client.{name}")
        cls = import_module("external.marcos_client.multiExp").MultiExperiment
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return cls


def _call_with_deadline(fn, timeout_s, timeout_message):
    """Run fn() in a worker thread and raise TimeoutError if it is still running after timeout_s.

    A blocked MaRCoS socket cannot be interrupted from Python, so the worker is a daemon
    thread that is abandoned on timeout; closing the board sockets releases it.
    None waits forever, like a direct call.
    """
    if timeout_s is None:
        return fn()
    outcome = {}

    def worker():
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # re-raised in the caller's thread
            outcome["error"] = exc

    thread = threading.Thread(target=worker, name="marcos-multiboard", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError(timeout_message)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _close_board_connections(expt):
    """Close the socket of every board in a MultiExperiment.

    shutdown() also wakes a thread blocked in recv() on that socket. Sockets the caller
    supplied through prev_socket are left alone.
    """
    for board in getattr(expt, "exps", {}).values():
        sock = getattr(board, "_s", None)
        if sock is None or not getattr(board, "_close_socket", True):
            continue
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        board._s = None


def _align_follower_events(events, shift_us, board_id):
    """Move a follower's events shift_us earlier.

    A follower starts its program when board 0's trigger arrives and then waits its own
    initial_wait before its first event. Moving its events earlier by trig_delay plus that
    wait puts its receive windows at the same moments as board 0's, to within the
    trigger's hardware latency.
    """
    aligned = {}
    for key, (times, values) in events.items():
        times = np.asarray(times, dtype=float) - shift_us
        values = np.asarray(values)
        early = times < 0
        if np.any(values[early] != 0):
            raise ValueError(
                f"Board {board_id}: {key} switches on less than {shift_us:.1f} us after the "
                "sequence starts, so it cannot be moved earlier to line up with board 0. "
                "Add delay before the first ADC event or set MARCOS_ALIGN_FOLLOWERS = False."
            )
        if np.any(early):
            keep = ~early
            times, values = times[keep], values[keep]
            if not np.any(times == 0):
                times = np.concatenate(([0.0], times))
                values = np.concatenate(([0], values))
        aligned[key] = (times, values)
    return aligned


def _route_tx(events, tx_channels):
    """Play the RF pulse on board 0's selected TX outputs; Pulseq always gives it as tx0."""
    tx_channels = tuple(tx_channels) or (0,)
    if tx_channels == (0,) or "tx0" not in events:
        return events
    routed = {key: value for key, value in events.items() if key not in ("tx0", "tx1")}
    times, values = events["tx0"]
    for channel in tx_channels:
        routed[f"tx{channel}"] = (np.array(times, copy=True), np.array(values, copy=True))
    return routed


def _sequence_length_s(board_instructions):
    """Latest event time across all boards, in seconds."""
    latest = 0.0
    for events in board_instructions.values():
        for times, _ in events.values():
            if len(times):
                latest = max(latest, float(np.max(times)))
    return latest / 1e6


# TODO: Remove references to cfg class from here
def run_pulseq(
    seq_file,
    rf_center=None,
    rf_max=None,
    gx_max=None,
    gy_max=None,
    gz_max=None,
    tx_t=1,
    grad_t=10,
    tx_warmup=100,
    shim_x=None,
    shim_y=None,
    shim_z=None,
    grad_cal=False,
    save_np=False,
    save_mat=False,
    save_msgs=False,
    expt=None,
    plot_instructions=False,
    gui_test=False,
    case_path="/tmp",
    raw_filename="",
    expected_duration_sec=-1,
    hardware_simulation=None,
    system=None,
    boards=None,
):
    """
    Interpret pulseq .seq file through flocra_pulseq

    Args:
        seq_file (string): Pulseq file in mgh.config SEQ file directory
        rf_center (float): [MHz] Center for frequency (larmor)
        rf_max (float): [Hz] Maximum system RF value for instruction scaling
        g[x, y, z]_max (float): [Hz/m] Maximum system gradient values for x, y, z for instruction scaling
        tx_t (float): [us] Raster period for transmit
        grad_t (float): [us] Raster period for gradients
        tx_warmup (float): [us] Warmup time for transmit gate, used to check pulseq file
        shim_x, shim_y, shim_z (float): Shim value, defaults to config SHIM_ values, must be less than 1 magnitude
        grad_cal (bool): Default False, run GPA_FHDO gradient calibration
        save_np (bool): Default False, save data in .npy format in mgh.config DATA directory
        save_mat (bool): Default False, save data in .mat format in mgh.config DATA directory
        save_msgs (bool): Default False, save log messages in .txt format in mgh.config DATA directory
        expt (flocra_pulseq.interpreter): Default None, pass in existing experiment to continue an object
        plot_instructions (bool): Default None, plot instructions for debugging
        gui_test (bool): Default False, load dummy data for gui testing
        system (pp.Opts): Default None, system configuration for pypulseq
        boards (dict): Optional board IDs mapped to connection settings and rx_channels.
    Returns:
        numpy.ndarray or dict: Legacy RX0 array or receiver arrays keyed by board and channel.
        float: (us) Rx period
    """
    try:
        cfg.update()
    except Exception:
        pass

    rf_center = cfg.LARMOR_FREQ if rf_center is None else rf_center
    rf_max = cfg.RF_MAX if rf_max is None else rf_max
    gx_max = cfg.GX_MAX if gx_max is None else gx_max
    gy_max = cfg.GY_MAX if gy_max is None else gy_max
    gz_max = cfg.GZ_MAX if gz_max is None else gz_max
    shim_x = cfg.SHIM_X if shim_x is None else shim_x
    shim_y = cfg.SHIM_Y if shim_y is None else shim_y
    shim_z = cfg.SHIM_Z if shim_z is None else shim_z

    log.info("Pulseq scan with Larmor %s", rf_center)
    log.info(
        "Full-scale calibrations (DAC ±1): RF max %s Hz; Gx %s Gy %s Gz %s Hz/m",
        rf_max,
        gx_max,
        gy_max,
        gz_max,
    )

    if hardware_simulation is None:
        try:
            import common.config as config

            hardware_simulation = config.get_config().is_hardware_simulation()
        except Exception:
            hardware_simulation = False

    log.debug("Running flocra_pulseq using following parameters:")
    log.debug("rf_center=%s rf_max=%s gx_max=%s gy_max=%s gz_max=%s", rf_center, rf_max, gx_max, gy_max, gz_max)
    log.debug("shim_x=%s shim_y=%s shim_z=%s seq=%s", shim_x, shim_y, shim_z, seq_file)
    log.info("case path = %s", case_path)

    try:
        from external.marcos_client.local_config import apply_scanner_settings
        from external.marcos_client import local_config as marcos_cfg

        apply_scanner_settings()
        clk_freq = float(marcos_cfg.fpga_clk_freq_MHz)
    except Exception as exc:
        log.warning("Could not apply MaRCoS settings: %s", exc)
        clk_freq = 122.88

    if system is None:
        import pypulseq as pp

        g_max = max(float(gx_max), float(gy_max), float(gz_max), 1.0)
        system = pp.Opts(
            max_grad=g_max,
            grad_unit="Hz/m",
            rf_ringdown_time=20e-6,
            rf_dead_time=100e-6,
            adc_dead_time=20e-6,
            rf_raster_time=max(float(tx_t), 1.0) * 1e-6,
            grad_raster_time=max(float(grad_t), 1.0) * 1e-6,
            block_duration_raster=1e-6,
        )

    # Single-board sequences record on board 0's first MRI receiver (RX0 by default).
    single_rx = 0
    if boards is None:
        single_rx = getattr(cfg, "MARCOS_SINGLE_BOARD_RX", 0)
        if single_rx is None:
            raise ValueError(
                "Board 0 has no receiver marked MRI. Sequences that record on one channel use "
                "board 0: mark its RX0 or RX1 as MRI in Settings > Imaging Console > MaRCoS boards."
            )

    psi = seq2flocra(
        center_freq=rf_center * 1e6,
        rf_amp_max=rf_max,
        system=system,
        clk_freq=clk_freq,
        gx_max=gx_max,
        gy_max=gy_max,
        gz_max=gz_max,
        rx_channels=(0, 1) if boards is not None else (single_rx,),
    )
    psi.load_seqfile(seq_file)
    psi.block_events_to_amps_times()
    instructions = psi._flo_dict
    log.info("GPA grad_t=%s us", psi._grad_t)

    try:
        instructions = shim(instructions, (shim_x, shim_y, shim_z))
    except Exception as exc:
        log.warning("Could not apply shim offsets: %s", exc)

    instructions = _route_tx(instructions, getattr(cfg, "MARCOS_TX_CHANNELS", (0,)))

    if boards is not None:
        endpoints = cfg.marcos_board_endpoints(boards)
        board_settings, board_instructions, selected = {}, {}, {}
        for board_id, settings in boards.items():
            options = dict(settings)
            selected[board_id] = normalize_rx_channels(
                options.pop("rx_channels", (0,) if board_id == 0 else (0, 1))
            )
            options["ip"], options["port"] = endpoints[board_id]
            options.setdefault("lo_freq", rf_center)
            if not np.allclose(options.get("rx_t", psi._rx_t), psi._rx_t):
                raise ValueError("All boards must use the Pulseq ADC sample period")
            options["rx_t"] = psi._rx_t
            board_settings[board_id] = options
            # only primary receives tx and gradient instructions.
            events = {key: (times.copy(), values.copy())
                      for key, (times, values) in instructions.items()} if board_id == 0 else {}
            for channel in (0, 1):
                times, values = instructions[f"rx{channel}_en"]
                events[f"rx{channel}_en"] = (
                    times.copy(), values.copy() if channel in selected[board_id]
                else np.zeros_like(values),
                )
            board_instructions[board_id] = events
        if not any(selected.values()):
            raise ValueError("Select at least one receive channel")
        # Keep the existing instruction plot showing the primarys sequence
        instructions = board_instructions[0]

    if plot_instructions:
        plt.clf()
        _, axs = plt.subplots(3, 1, sharex="col", constrained_layout=True)
        for key in ["tx0", "tx1"]:
            if key in instructions:
                axs[0].step(instructions[key][0], abs(instructions[key][1]), where="post", label=key)
        for key in ([f"rx{single_rx}_en"] if boards is None else ["rx0_en", "rx1_en"]):
            if key in instructions:
                axs[1].step(instructions[key][0], instructions[key][1], where="post", label=key)
        for key in ["grad_vx", "grad_vy", "grad_vz", "grad_vz2"]:
            if key in instructions:
                axs[2].step(instructions[key][0], instructions[key][1], where="post", label=key)
        for ax in axs:
            ax.legend()
            ax.grid(True, color="#333")

    if hardware_simulation:
        log.info("Hardware simulation set. Skipping scan.")
        if boards is not None:
            return {board_id: {f"rx{ch}": np.array([], dtype=np.complex128)
                               for ch in channels}
                    for board_id, channels in selected.items()}, psi._rx_t
        return np.array([], dtype=np.complex128), psi._rx_t

    owns_expt = expt is None
    if expt is None:
        from external.marcos_client import local_config as marcos_cfg
        from sequences.common.util import reading_json_parameter

        init_gpa = False
        try:
            init_gpa = bool(reading_json_parameter().marcos_parameters.initialize_gpa)
        except Exception:
            init_gpa = False

        if boards is None:
            log.info(
                "Connecting to MaRCoS at %s:%s (init_gpa=%s, RX%s, TX%s)",
                marcos_cfg.ip_address,
                marcos_cfg.port,
                init_gpa,
                single_rx,
                "+".join(str(ch) for ch in getattr(cfg, "MARCOS_TX_CHANNELS", (0,))),
            )
        try:
            if boards is None:
                board0 = getattr(cfg, "MARCOS_BOARD_ADDRESSES", {}).get(0, {})
                expt = ex.Experiment(
                    lo_freq=rf_center,
                    rx_t=psi._rx_t,
                    init_gpa=init_gpa,
                    gpa_fhdo_offset_time=psi._grad_t / 3,
                    grad_max_update_rate=0.0625,
                    halt_and_reset=True,
                    set_cic_shift=bool(board0.get("set_cic_shift", False)),
                )
            else:
                MultiExperiment = load_multi_experiment(boards)

                for board_id, options in board_settings.items():
                    if board_id == 0:
                        options.setdefault("init_gpa", init_gpa)
                    else:
                        options["init_gpa"] = False
                    options.setdefault("grad_max_update_rate", 0.0625)
                    options.setdefault("halt_and_reset", True)
                board_settings[0].setdefault("gpa_fhdo_offset_time", psi._grad_t / 3)
                connect_timeout = getattr(cfg, "MARCOS_CONNECT_TIMEOUT_S", None)
                expt = _call_with_deadline(
                    lambda: MultiExperiment(boards=board_settings),
                    connect_timeout,
                    f"Boards {sorted(board_settings)} did not finish connecting within "
                    f"{connect_timeout} s. A marcos_server still stuck in an earlier scan "
                    "accepts the connection but never answers. Restart marcos_server on "
                    "each board, then retry.",
                )
        except ConnectionError:
            raise
        except OSError as exc:
            if boards is not None:
                raise
            hint = (
                " Turn off Initialize GPA in Settings if no gradient board is attached."
                if init_gpa
                else ""
            )
            raise ConnectionError(
                f"MaRCoS at {marcos_cfg.ip_address}:{marcos_cfg.port} dropped the connection: {exc}.{hint}"
            ) from exc

    
    # Optionally run gradient linearization calibration
    if grad_cal:
        gradient_expt = expt if boards is None else expt.exps[0]
        gradient_expt.gradb.calibrate(
            channels=[0, 1, 2],
            max_current=1,
            num_calibration_points=30,
            averages=5,
            poly_degree=5,
        )

    # Load instructions
    # instructions = {
    #     "tx0": psi._flo_dict['tx0'],
    #     "tx_gate": psi._flo_dict['tx_gate'],
    #     "rx0_en": psi._flo_dict['rx0_en'],  # adc 0
    #     "grad_vx": psi._flo_dict['grad_vx'],
    #     "grad_vy": psi._flo_dict['grad_vy'],
    #     "grad_vz": psi._flo_dict['grad_vz'],
    #     }

    if boards is not None and getattr(cfg, "MARCOS_ALIGN_FOLLOWERS", False):
        trig_delay = cfg.MARCOS_TRIGGER[0]
        for board_id in board_instructions:
            if board_id == 0:
                continue
            shift = trig_delay + expt.exps[board_id]._initial_wait
            board_instructions[board_id] = _align_follower_events(
                board_instructions[board_id], shift, board_id
            )
            log.info("Board %s receive events moved %.3f us earlier to line up with board 0",
                     board_id, shift)

    expt.add_flodict(instructions if boards is None else board_instructions)


    # if plot_instructions:
    #     expt.plot_sequence()

    log.debug("Running instructions...")

    if expected_duration_sec > 0:
        ipc_comm.send_acq_data(helper.get_datetime(), expected_duration_sec, False)

    # Run experiment
   
    log.debug('instructions:.......')
    # log.debug(instructions)

    if boards is None:
        rxd, msgs = expt.run()
    else:
        run_timeout = getattr(cfg, "MARCOS_RUN_TIMEOUT_S", None)
        message = ""
        if run_timeout is not None:
            trig_delay, trig_length = cfg.MARCOS_TRIGGER
            run_timeout += _sequence_length_s(board_instructions) + (trig_delay + trig_length) / 1e6
            followers = ", ".join(
                f"{board_id} ({board_settings[board_id]['ip']})"
                for board_id in board_settings if board_id != 0
            )
            message = (
                f"The multi-board run did not finish within {run_timeout:.1f} s. Most likely "
                f"follower board(s) {followers or 'none'} never saw board 0's trigger and are "
                "still waiting. Check the trigger wire, then restart marcos_server on those "
                "boards before the next scan."
            )
        try:
            rxd, msgs = _call_with_deadline(
                lambda: expt.run(*cfg.MARCOS_TRIGGER), run_timeout, message
            )
        except TimeoutError:
            _close_board_connections(expt)
            raise
        if owns_expt:
            _close_board_connections(expt)
    if boards is not None:
        rxd = {board_id: {f"rx{ch}": rxd[board_id][f"rx{ch}"] for ch in channels}
               for board_id, channels in selected.items()}

    # log.info('rxd shape:', rxd["rx0"].shape)

    # Optionally save messages
    if save_msgs:
        log.info("Received MaRCoS messages: %s", msgs)

    # Announce completion
    
    if not raw_filename:
        from datetime import datetime

        now = datetime.now()
        raw_filename = now.strftime("%y-%d-%m %H_%M_%S")

    # Optionally save rx output array as .npy file
    if save_np and boards is None:
        filename = Path(case_path) / mri4all_taskdata.RAWDATA / f"{raw_filename}.npy"
        if os.path.exists(filename):
            os.remove(filename)
        np.save(filename, rxd[f"rx{single_rx}"])

    # Optionally save rx output array as .mat file
    if save_mat and boards is None:
        filename = Path(case_path) / mri4all_taskdata.RAWDATA / f"{raw_filename}.mat"
        if os.path.exists(filename):
            os.remove(filename)
        sio.savemat(filename, {"flocra_data": rxd[f"rx{single_rx}"]})

    if boards is not None:
        for board_id, channels in rxd.items():
            for channel, data in channels.items():
                name = f"{raw_filename}_board{board_id}_{channel}"
                if save_np:
                    filename = Path(case_path) / mri4all_taskdata.RAWDATA / f"{name}.npy"
                    np.save(filename, data)
                if save_mat:
                    filename = Path(case_path) / mri4all_taskdata.RAWDATA / f"{name}.mat"
                    sio.savemat(filename, {"flocra_data": data})

    # Very dangerous to call the destructor here!
    # TODO: Check why this is needed and potential impact
    if boards is None:
        expt.__del__()


    # Return rx output array and rx period
    return rxd[f"rx{single_rx}"] if boards is None else rxd, psi._rx_t



def shim(instructions, shim):
    """
    Modify gradient instructions to shim the gradients for the experiment

    Args:
        instructions (dict): Instructions to modify
        shim (tuple): X, Y, Z shim values to use

    Returns:
        dict: Shimmed instructions
    """
    grads = ["grad_vx", "grad_vy", "grad_vz"]
    for ch in range(3):
        if grads[ch] not in instructions:
            continue
        updates = instructions[grads[ch]][1]
        updates[:-1] = updates[:-1] + shim[ch]
        assert np.all(np.abs(updates) <= 1), (
            f"Shim {shim[ch]} was too large for {grads[ch]}: "
            + f"{updates[np.argmax(np.abs(updates))]}"
        )
        instructions[grads[ch]] = (instructions[grads[ch]][0], updates)
    return instructions


def recon_0d(rxd, rx_t, trs=1, larmor_freq=cfg.LARMOR_FREQ):
    """
    Reconstruct FFT data, pass data out to plotting or saving programs

    Args:
        rxd (numpy.ndarray): Rx data array
        rx_t (float): [us] Rx sample period
        trs (int): Number of repetitions to split apart
        larmor_freq (float): [MHz] Larmor frequency of data for FFT

    Returns:
        dict: Useful reconstructed data dictionary
    """
    # Split echos for FFT
    rx_arr = np.reshape(rxd, (trs, -1)).T
    rx_fft = np.fft.fftshift(
        np.fft.fft(np.fft.fftshift(rx_arr, axes=(0,)), axis=0), axes=(0,)
    )
    x = np.linspace(
        0, rx_arr.shape[0] * rx_t * 1e-6, num=rx_arr.shape[0], endpoint=False
    )

    fft_bw = 1 / (rx_t)
    fft_x = np.linspace(
        larmor_freq - fft_bw / 2, larmor_freq + fft_bw / 2, num=rx_fft.shape[0]
    )
    out_dict = {
        "dim": 0,
        "rxd": rxd,
        "rx_t": rx_t,
        "trs": trs,
        "rx_arr": rx_arr,
        "rx_fft": rx_fft,
        "x": x,
        "fft_bw": fft_bw,
        "fft_x": fft_x,
        "larmor_freq": larmor_freq,
    }
    return out_dict


def recon_1d(rxd, rx_t, trs=1, larmor_freq=cfg.LARMOR_FREQ):
    """
    Reconstruct 1D data, pass data out to plotting or saving programs

    Args:
        rxd (numpy.ndarray): Rx data array
        rx_t (float): [us] Rx sample period
        trs (int): Number of repetitions to split apart
        larmor_freq (float): [MHz] Larmor frequency of data for FFT

    Returns:
        dict: Useful reconstructed data dictionary
    """
    # Split echos for FFT
    rx_arr = np.reshape(rxd, (trs, -1)).T
    rx_fft = np.fft.fftshift(
        np.fft.fft(np.fft.fftshift(rx_arr, axes=(0,)), axis=0), axes=(0,)
    )
    x = np.linspace(
        0, rx_arr.shape[0] * rx_t * 1e-6, num=rx_arr.shape[0], endpoint=False
    )

    fft_bw = 1 / (rx_t)
    fft_x = np.linspace(
        larmor_freq - fft_bw / 2, larmor_freq + fft_bw / 2, num=rx_fft.shape[0]
    )
    out_dict = {
        "dim": 1,
        "rxd": rxd,
        "rx_t": rx_t,
        "trs": trs,
        "rx_arr": rx_arr,
        "rx_fft": rx_fft,
        "x": x,
        "fft_bw": fft_bw,
        "fft_x": fft_x,
        "larmor_freq": larmor_freq,
    }
    return out_dict


def recon_2d(rxd, trs, larmor_freq=cfg.LARMOR_FREQ):
    """
    Reconstruct 2D data, pass data out to plotting or saving programs

    Args:
        rxd (numpy.ndarray): Rx data array
        trs (int): Number of repetitions (Phase encode direction length)
        larmor_freq (float): [MHz] Larmor frequency of data for FFT

    Returns:
        dict: Useful reconstructed data dictionary
    """
    rx_arr = np.reshape(rxd, (trs, -1))
    rx_fft = np.fft.fftshift(np.fft.fft2(np.fft.fftshift(rx_arr)))
    out_dict = {
        "dim": 2,
        "rxd": rxd,
        "trs": trs,
        "rx_arr": rx_arr,
        "rx_fft": rx_fft,
        "larmor_freq": larmor_freq,
    }
    return out_dict


def peak_width_1d(recon_dict):
    """
    Find peak width from reconstructed data

    Args:
        recon_dict (dict): Reconstructed data dictionary

    Returns:
        dict: Line info dictionary
    """
    rx_fft, fft_x = itemgetter("rx_fft", "fft_x")(recon_dict)

    peaks, _ = sig.find_peaks(np.abs(rx_fft), width=2)
    peak_results = sig.peak_widths(np.abs(rx_fft), peaks, rel_height=0.95)
    max_peak = np.argmax(peak_results[0])
    fwhm = peak_results[0][max_peak]

    hline = np.array(
        [
            peak_results[1][max_peak],
            peak_results[2][max_peak],
            peak_results[3][max_peak],
        ]
    )
    hline[1:] = hline[1:] * (fft_x[1] - fft_x[0]) + fft_x[0]
    out_dict = {
        "hline": hline,
        "fwhm": fwhm,
    }
    return out_dict


def plot_signal_1d(recon_dict):
    # Example plotting function
    # Split echos for FFT
    x, rxd, rx_arr, rx_fft, fft_x = itemgetter("x", "rxd", "rx_arr", "rx_fft", "fft_x")(
        recon_dict
    )

    _, axs = plt.subplots(4, 1, constrained_layout=True)
    axs[0].plot(np.real(rxd))
    axs[0].set_title("Concatenated signal -- Real")
    axs[1].plot(np.abs(rxd))
    axs[1].set_title("Concatenated signal -- Magnitude")
    axs[2].plot(x, np.angle(rx_arr))
    axs[2].set_title("Stacked signals -- Phase")
    axs[3].plot(fft_x, np.abs(rx_fft))
    axs[3].set_title("Stacked signals -- FFT")
    plt.show()


def plot_signal_2d(recon_dict):
    # Example plotting function
    rx_arr, rx_fft, rxd = itemgetter("rx_arr", "rx_fft", "rxd")(recon_dict)

    _, axs = plt.subplots(3, 1, constrained_layout=True)
    axs[0].plot(np.real(rxd))
    axs[0].set_title("Concatenated signal -- Real")
    axs[1].plot(np.abs(rxd))
    axs[1].set_title("Concatenated signal -- Magnitude")
    axs[2].plot(np.abs(rx_arr))
    axs[2].set_title("Stacked signals -- Magnitude")
    fig, im_axs = plt.subplots(1, 2, constrained_layout=True)
    fig.suptitle("2D Image")
    im_axs[0].imshow(np.abs(rx_fft), cmap=plt.cm.bone)
    im_axs[0].set_title("Magnitude")
    im_axs[1].imshow(np.angle(rx_fft))
    im_axs[1].set_title("Phase")
    plt.show()


if __name__ == "__main__":
    # Maybe clean up
    if len(sys.argv) >= 2:
        command = sys.argv[1]
        if command == "pulseq":
            if len(sys.argv) == 3:
                seq_file = cfg.SEQ_PATH + sys.argv[2]
                _, rx_t = run_pulseq(seq_file, save_np=True, save_mat=True)
                log.debug(f"rx_t = {rx_t}")
            else:
                log.debug(
                    '"pulseq" takes one .seq filename as an argument (just the filename, make sure it\'s in your seq_files path!)'
                )
        elif command == "plot2d":
            if len(sys.argv) == 4:
                rxd = np.load(cfg.DATA_PATH + sys.argv[2])
                tr_count = int(sys.argv[3])
                plot_signal_2d(recon_2d(rxd, tr_count, larmor_freq=cfg.LARMOR_FREQ))
            else:
                log.debug('Format arguments as "plot2d [2d_data_filename] [tr count]"')
        elif command == "plot1d":
            if len(sys.argv) == 5:
                rxd = np.load(cfg.DATA_PATH + sys.argv[2])
                rx_t = float(sys.argv[3])
                tr_count = int(sys.argv[4])
                plot_signal_1d(recon_1d(rxd, rx_t, trs=tr_count))
            else:
                log.debug(
                    'Format arguments as "plot1d [1d_data_filename] [rx_t] [tr_count]"'
                )
        elif command == "plot_se":
            if len(sys.argv) == 5:
                rxd = np.load(cfg.DATA_PATH + sys.argv[2])
                rx_t = float(sys.argv[3])
                tr_count = int(sys.argv[4])
                plot_signal_1d(recon_0d(rxd, rx_t, trs=tr_count))
            else:
                log.debug(
                    'Format arguments as "plot_se [spin_echo_data_filename] [rx_t] [tr_count]"'
                )

        else:
            log.debug("Enter a script command from: [pulseq, plot_se, plot1d, plot2d]")
    else:
        log.debug("Enter a script command from: [pulseq, plot_se, plot1d, plot2d]")
