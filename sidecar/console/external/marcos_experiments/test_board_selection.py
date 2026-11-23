#!/usr/bin/env python3
"""Check that the boards and receive channels you select are the ones that actually run.

Uses the console's own config, board loader, alignment and timeout helpers and your
teammate's MultiExperiment, so it exercises the same code as a multi-board FID scan.

Close the Adelpha app first: marcos_server answers one client at a time.
Run from sidecar/console with the console's Python environment active:

    python external/marcos_experiments/test_board_selection.py
        boards and channels from MARCOS_BOARDS in config.py
    python external/marcos_experiments/test_board_selection.py --select 0:1 1:0,1
        board 0 RX1 only; board 1 RX0 and RX1 (boards not listed are left out)
    python external/marcos_experiments/test_board_selection.py --select 0: 1:0
        board 0 only sends the trigger; board 1 RX0
    python external/marcos_experiments/test_board_selection.py --loopback --plot loopback.png
        also send a small TX0 pulse from board 0 and compare where it lands on every
        selected stream. Needs TX0 -> attenuator -> splitter -> the RX inputs you selected.
        Never run --loopback with TX0 wired to the RF amplifier or coil.

How the check works
  * Each possible stream (board, RX channel) gets a receive window of a different length,
    so the number of samples that comes back identifies which board and channel made it.
    Unselected channels are programmed off and must return nothing.
  * Each board's program is inspected before it runs: receive enables, board 0's trigger
    output, and the follower's leading wait-for-trigger instruction.
  * Each board's run is timed. A follower that never finishes means it never saw board 0's
    trigger. A follower that finishes before board 0 even starts did not wait for it.

Exit codes: 0 everything matched, 1 selection mismatch, 2 trigger or connection problem.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import io
import os
import socket
import sys
import time
from pathlib import Path

CONSOLE = Path(__file__).resolve().parents[2]  # sidecar/console
EXPECTED_ONSET_US = 50.0  # loopback pulse starts this long after every window opens
PULSE_US = 20.0
TOLERANCE_SAMPLES = 2

np = msgpack = cfg = scripts = marcos_cfg = None  # filled in by import_console()
normalize_rx_channels = construct_packet = ITRIGFOREVER = None


def resolve_base(cli_value):
    """Same folder choice as `python -m services.api`; the desktop app passes MRI4ALL_BASE."""
    if cli_value:
        return Path(cli_value)
    if os.getenv("MRI4ALL_BASE"):
        return Path(os.environ["MRI4ALL_BASE"])
    if Path("/opt/mri4all").is_dir():
        return Path("/opt/mri4all")
    return CONSOLE.parent / ".mri4all"


def import_console():
    global np, msgpack, cfg, scripts, marcos_cfg, normalize_rx_channels, construct_packet, ITRIGFOREVER
    if str(CONSOLE) not in sys.path:
        sys.path.insert(0, str(CONSOLE))
    import numpy as np  # noqa: F811
    import msgpack  # noqa: F811
    import external.seq.adjustments_acq.config as cfg  # noqa: F811
    import external.seq.adjustments_acq.scripts as scripts  # noqa: F811
    # scripts.py opens the acquisition IPC endpoint on import and deletes its pipe file at
    # exit; keep that file in place in case the console is running.
    atexit.unregister(scripts.ipc_comm.cleanup)
    from external.flocra_pulseq.interpreter_pp import normalize_rx_channels  # noqa: F811
    from external.marcos_client import local_config as marcos_cfg  # noqa: F811
    from external.marcos_client.marmachine import ITRIGFOREVER  # noqa: F811
    from external.marcos_client.server_comms import construct_packet  # noqa: F811
    globals().update(np=np, msgpack=msgpack, cfg=cfg, scripts=scripts, marcos_cfg=marcos_cfg,
                     normalize_rx_channels=normalize_rx_channels,
                     construct_packet=construct_packet, ITRIGFOREVER=ITRIGFOREVER)


# ---------------------------------------------------------------- selection

def parse_select(items):
    selection = {}
    for item in items:
        board, sep, chans = item.partition(":")
        try:
            if not sep:
                raise ValueError
            board_id = int(board)
            channels = tuple(int(c) for c in chans.split(",") if c.strip())
        except ValueError:
            raise SystemExit(f"--select expects BOARD:CHANNELS such as 0:0,1  1:1  or  0:  (got {item!r})")
        if board_id in selection:
            raise SystemExit(f"Board {board_id} is listed twice in --select")
        selection[board_id] = channels
    return selection


def build_boards(args):
    configured = {b: dict(s) for b, s in (cfg.MARCOS_BOARDS or {}).items()}
    for spec in args.ip:
        board, sep, address = spec.partition("=")
        host, _, port = address.partition(":")
        if not sep or not board.strip().isdigit() or not host:
            raise SystemExit(f"--ip expects BOARD=ADDRESS[:PORT] (got {spec!r})")
        entry = configured.setdefault(int(board), {})
        entry["ip"] = host
        entry["port"] = int(port) if port else entry.get("port", 11111)
    if args.select:
        selection = parse_select(args.select)
    else:
        selection = {b: s.get("rx_channels", (0,) if b == 0 else (0, 1)) for b, s in configured.items()}
    if not selection:
        raise SystemExit("No boards configured: set MARCOS_BOARDS in config.py or pass --ip and --select")
    if 0 not in selection:
        raise SystemExit("Board 0 must be included: it is the controller that sends the trigger. "
                         "Use '0:' to include it without receiving.")
    boards = {}
    for board_id in sorted(selection):
        if board_id not in configured or "ip" not in configured[board_id]:
            raise SystemExit(f"Board {board_id} has no address. Add it to MARCOS_BOARDS in config.py "
                             f"or pass --ip {board_id}=ADDRESS")
        settings = dict(configured[board_id])
        try:
            settings["rx_channels"] = normalize_rx_channels(selection[board_id])
        except ValueError as exc:
            raise SystemExit(f"Board {board_id}: {exc}")
        boards[board_id] = settings
    try:
        cfg.marcos_board_endpoints(boards)  # the console's own validation
    except ValueError as exc:
        raise SystemExit(str(exc))
    return boards


def plan_streams(boards, base_samples, rx_t, t0):
    """Give every possible (board, channel) a window of a unique length."""
    plan = {}
    for k, (board_id, ch) in enumerate((b, c) for b in sorted(boards) for c in (0, 1)):
        samples = base_samples * (k + 1)
        plan[(board_id, ch)] = dict(samples=samples, selected=ch in boards[board_id]["rx_channels"],
                                    start=t0, end=t0 + samples * rx_t)
    return plan


def build_events(boards, plan, args, t0):
    events = {}
    for board_id in boards:
        ev = {}
        for ch in (0, 1):
            p = plan[(board_id, ch)]
            on = 1.0 if p["selected"] else 0.0  # unselected: programmed off, like run_pulseq
            ev[f"rx{ch}_en"] = (np.array([0.0, p["start"], p["end"]]), np.array([0.0, on, 0.0]))
        if board_id == 0 and args.loopback:
            a = t0 + EXPECTED_ONSET_US
            # tx0 only; tx_gate stays low so a gated RF amplifier stays blanked
            ev["tx0"] = (np.array([0.0, a, a + PULSE_US]), np.array([0, args.tx_amp, 0], dtype=complex))
        events[board_id] = ev
    return events


# ---------------------------------------------------------------- board helpers

def handshake(ip, port, timeout):
    """One complete MaRCoS request/reply, like the console's probe (never open-and-drop)."""
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except OSError as exc:
        return "unreachable", f"cannot connect ({exc})"
    try:
        sock.sendall(msgpack.packb(construct_packet({"regstatus": 1})))
        unpacker = msgpack.Unpacker()
        while True:
            buf = sock.recv(4096)
            if not buf:
                return "closed", "closed the connection without answering"
            unpacker.feed(buf)
            for _ in unpacker:
                return "ok", "answers"
    except socket.timeout:
        return "stuck", (f"accepts connections but did not answer within {timeout:g} s; marcos_server "
                         "is probably still waiting in an earlier run (e.g. for a trigger)")
    except OSError as exc:
        return "error", str(exc)
    finally:
        sock.close()


def manual_restart(ip):
    return (f"ssh root@{ip}   then:   killall marcos_server; "
            "nohup ~/marcos_server >/tmp/marcos_server.log 2>&1 &")


def restart_server(board_id, ip, port):
    """Restart only the marcos_server process over SSH; the loaded FPGA bitstream is left alone."""
    print(f"  Restarting marcos_server on board {board_id} ({ip})...", flush=True)
    try:
        from services.ui.marcos_boot import RpSession, _start_server, _wait_for_server
        with RpSession(ip) as session:
            _start_server(session)
        ok = _wait_for_server(ip, port)
    except Exception as exc:  # SSH problems, missing paramiko, ...
        print(f"  Restart failed: {exc}\n  Restart it by hand: {manual_restart(ip)}")
        return False
    print("  marcos_server is answering again." if ok else
          f"  Started it, but port {port} is not answering yet. Check /tmp/marcos_server.log on the board.")
    return ok


def preflight(boards, timeout, restart):
    problems = []
    for board_id, s in boards.items():
        state, detail = handshake(s["ip"], s.get("port", 11111), timeout)
        print(f"  board {board_id}  {s['ip']}:{s.get('port', 11111)}  {detail}")
        if state == "stuck" and restart and restart_server(board_id, s["ip"], s.get("port", 11111)):
            state, detail = handshake(s["ip"], s.get("port", 11111), timeout)
            print(f"  board {board_id}  {detail}")
        if state != "ok":
            problems.append((board_id, state))
    for board_id, state in problems:
        ip = boards[board_id]["ip"]
        if state == "stuck":
            print(f"  -> Restart marcos_server on board {board_id}: rerun with --restart-hung, or {manual_restart(ip)}")
        else:
            print(f"  -> Board {board_id} is not reachable or marcos_server is not running there.")
    return not problems


def board_options(board_id, settings, args):
    options = {k: v for k, v in settings.items() if k != "rx_channels"}
    options.setdefault("lo_freq", args.lo)
    options["rx_t"] = args.rx_t
    options["init_gpa"] = False  # no gradients in this test
    options.setdefault("grad_max_update_rate", 0.0625)
    options.setdefault("halt_and_reset", True)
    return options


def time_runs(multi):
    """Record when each board's run() starts and ends (MultiExperiment calls exp.run)."""
    timing = {}
    for board_id, exp in multi.exps.items():
        original = exp.run

        def timed(original=original, board_id=board_id):
            timing[board_id] = {"start": time.perf_counter()}
            try:
                return original()
            finally:
                timing[board_id]["end"] = time.perf_counter()

        exp.run = timed
    return timing


def programmed(exp, key):
    times, values = exp._seq.get(key, ((), ()))
    return bool(np.any(np.asarray(values) != 0))


def pulse_onset_us(data, rx_t):
    """Time from window start to the pulse's half-height crossing, or None if no pulse."""
    mag = np.abs(np.asarray(data))
    # baseline from the part of the window after the pulse, which is pulse-free whether or
    # not the follower windows are aligned
    tail = mag[int((EXPECTED_ONSET_US + PULSE_US + 50) / rx_t):]
    if tail.size < 8:
        return None
    floor, noise = float(np.median(tail)), float(np.std(tail))
    peak = float(mag.max())
    if peak <= floor + max(10 * noise, 1e-9):
        return None
    half = floor + 0.5 * (peak - floor)
    i = int(np.argmax(mag >= half))
    if i == 0:
        return 0.0
    frac = (half - mag[i - 1]) / (mag[i] - mag[i - 1])
    return (i - 1 + frac) * rx_t


# ---------------------------------------------------------------- one run

def run_once(boards, args, trig, align, attempt):
    trig_delay, trig_length = trig
    rx_t = args.rx_t
    followers = [b for b in boards if b != 0]
    t0 = 100.0 + trig_delay  # leaves room to move follower windows earlier
    plan = plan_streams(boards, args.samples, rx_t, t0)
    events = build_events(boards, plan, args, t0)
    out = io.StringIO()
    quiet = contextlib.nullcontext() if args.verbose else contextlib.redirect_stdout(out)

    print(f"\n=== Run {attempt} ===")
    MultiExperiment = scripts.load_multi_experiment(boards)
    options = {b: board_options(b, s, args) for b, s in boards.items()}
    multi = None
    try:
        with quiet:
            try:
                multi = scripts._call_with_deadline(
                    lambda: MultiExperiment(boards=options), args.connect_timeout, "connect timeout")
            except TimeoutError:
                multi = None
                error = "timeout"
            except Exception as exc:
                error = exc
        if multi is None:
            if error == "timeout":
                print(f"FAIL: the boards did not finish connecting within {args.connect_timeout:g} s "
                      "(a marcos_server is probably stuck). Restart marcos_server on each board.")
            else:
                print(f"FAIL: could not connect: {error}")
            return 2

        if align and followers:
            for b in followers:
                shift = trig_delay + multi.exps[b]._initial_wait
                events[b] = scripts._align_follower_events(events[b], shift, b)
            print(f"Follower windows moved {trig_delay + multi.exps[followers[0]]._initial_wait:g} us "
                  "earlier to line up with board 0 (MARCOS_ALIGN_FOLLOWERS).")
        with quiet:
            multi.add_flodict(events)
            multi.compile(trig_delay=trig_delay, trig_length=trig_length)

        # ---- what each board was told to do
        problems, trigger_problems = [], []
        controller = multi.exps[0]
        if not programmed(controller, "trig_out"):
            problems.append("board 0's program has no trigger output")
        if controller._machine_code[0] >> 24 == ITRIGFOREVER:
            problems.append("board 0's program waits for a trigger; it should send one")
        for b in followers:
            if multi.exps[b]._machine_code[0] >> 24 != ITRIGFOREVER:
                problems.append(f"board {b}'s program does not start by waiting for the trigger")
        for (b, ch), p in plan.items():
            p["programmed"] = programmed(multi.exps[b], f"rx{ch}_en")
            if p["programmed"] != p["selected"]:
                problems.append(f"board {b} RX{ch} is {'' if p['programmed'] else 'not '}programmed "
                                f"but was {'' if p['selected'] else 'not '}selected")

        # ---- run, with a deadline and per-board timing
        timing = time_runs(multi)
        seq_s = (max(p["end"] for p in plan.values()) + trig_delay + trig_length) / 1e6
        deadline = args.timeout + seq_s
        rxd = None
        with quiet:
            try:
                rxd, _ = scripts._call_with_deadline(
                    lambda: multi.run(trig_delay, trig_length), deadline, "run timeout")
                error = None
            except TimeoutError:
                error = "timeout"
                # note who was still running before closing the sockets, which ends their run()
                controller_done = "end" in timing.get(0, {})
                hung = [b for b in followers if "end" not in timing.get(b, {})]
                armed_at = {b: timing.get(b, {}).get("start") for b in hung}
                controller_start = timing.get(0, {}).get("start")
                scripts._close_board_connections(multi)  # releases the blocked follower threads
                time.sleep(0.5)
            except Exception as exc:
                error = exc

        if error == "timeout":
            if not controller_done:
                print(f"FAIL: board 0 (controller) did not finish within {deadline:.1f} s. That happens "
                      "before any trigger is involved: check board 0's link and marcos_server.")
            else:
                for b in hung:
                    armed = (controller_start - armed_at[b]) if armed_at[b] is not None else float("nan")
                    print(f"TRIGGER NOT RECEIVED: board {b} ({boards[b]['ip']}) was armed "
                          f"{armed:.2f} s before board 0 started; board 0 finished and sent its "
                          f"trigger, but board {b} had still not finished {deadline:.1f} s later.")
                print("  Likely causes: trigger wire not connected (or on the wrong pins) from board 0's "
                      "trigger output to the follower's trigger input; no common ground between the "
                      "boards; the follower running a bitstream without trigger support.")
                print("  The follower's marcos_server is still waiting and will not answer anything "
                      "until it is restarted:")
                for b in hung:
                    if args.restart_hung:
                        restart_server(b, boards[b]["ip"], boards[b].get("port", 11111))
                    else:
                        print(f"    board {b}: rerun with --restart-hung, or {manual_restart(boards[b]['ip'])}")
            return 2
        if error is not None:
            print(f"FAIL: the run raised {type(error).__name__}: {error}")
            if not args.verbose and out.getvalue().strip():
                print("  Last output from MultiExperiment:\n    " +
                      "\n    ".join(out.getvalue().strip().splitlines()[-8:]))
            return 2

        # ---- did each follower wait for the trigger?
        c_start, c_end = timing[0]["start"], timing[0]["end"]
        for b in followers:
            t = timing[b]
            if t["end"] < c_start:
                trigger_problems.append(
                    f"board {b} finished {c_start - t['end']:.3f} s before board 0 even started, so it "
                    "did not wait for board 0's trigger. Its trigger input is probably floating or "
                    "picking up noise (check the wire and ground). Its data is NOT synchronized.")
            else:
                print(f"Trigger OK: board {b} waited for board 0 and finished "
                      f"{(t['end'] - c_end) * 1000:+.1f} ms relative to board 0.")
        if not followers:
            print("Only board 0 selected: the trigger is not exercised in this run.")

        # ---- what came back
        print(f"\n{'board':>5} {'ch':>3} {'selected':>8} {'programmed':>10} {'expected':>8} "
              f"{'received':>8} {'mean |x|':>10}  result")
        expected_by_count = {p["samples"]: k for k, p in plan.items()}
        for (b, ch), p in plan.items():
            data = np.asarray((rxd.get(b) or {}).get(f"rx{ch}", []))
            n = int(data.size)
            p["data"] = data
            if p["selected"]:
                if abs(n - p["samples"]) <= TOLERANCE_SAMPLES:
                    result = "OK"
                elif n == 0:
                    result = "NO DATA"
                    problems.append(f"board {b} RX{ch} was selected but returned no samples")
                else:
                    other = expected_by_count.get(n)
                    hint = f" (that is the window of board {other[0]} RX{other[1]})" if other else ""
                    result = "WRONG LENGTH" + hint
                    problems.append(f"board {b} RX{ch}: expected {p['samples']} samples, got {n}{hint}")
            else:
                result = "OK (off)" if n == 0 else "UNEXPECTED DATA"
                if n:
                    other = expected_by_count.get(n)
                    hint = f" (that is the window of board {other[0]} RX{other[1]})" if other else ""
                    problems.append(f"board {b} RX{ch} was not selected but returned {n} samples{hint}")
            level = f"{float(np.mean(np.abs(data))):.3g}" if n else "-"
            print(f"{b:>5} {ch:>3} {('yes' if p['selected'] else 'no'):>8} "
                  f"{('yes' if p['programmed'] else 'no'):>10} {p['samples'] if p['selected'] else 0:>8} "
                  f"{n:>8} {level:>10}  {result}")

        # ---- loopback timing across boards
        if args.loopback:
            print("\nLoopback pulse (time from window open to the pulse's half-height):")
            onsets = {}
            for (b, ch), p in plan.items():
                if p["selected"] and p["data"].size:
                    onset = pulse_onset_us(p["data"], rx_t)
                    onsets[(b, ch)] = onset
                    print(f"  board {b} RX{ch}: " + ("no pulse seen (is the loopback cable on this input?)"
                                                     if onset is None else f"{onset:.2f} us"))
            ref = next(((k, v) for k, v in onsets.items() if k[0] == 0 and v is not None), None)
            if ref is None:
                print("  No board 0 stream with the pulse to compare against.")
            else:
                for (b, ch), onset in onsets.items():
                    if b == 0 or onset is None:
                        continue
                    expected_offset = 0.0 if align else trig_delay + multi.exps[b]._initial_wait
                    # a follower window that opens later sees the pulse earlier in its window
                    offset = ref[1] - onset
                    note = "lines up" if abs(offset - expected_offset) <= rx_t else (
                        f"expected {expected_offset:.1f} us; trigger timing or alignment is off")
                    print(f"  board {b} RX{ch} window opens {offset:+.2f} us "
                          f"({offset / rx_t:+.1f} samples) relative to board 0 RX{ref[0][1]}: {note}")
                    if note != "lines up":
                        problems.append(f"board {b} RX{ch} is offset {offset:+.2f} us from board 0")

        if args.plot:
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(11, 5))
            for (b, ch), p in plan.items():
                if p["selected"] and p["data"].size:
                    ax.plot(p["start"] + np.arange(p["data"].size) * rx_t, np.abs(p["data"]),
                            label=f"board {b} RX{ch}")
            ax.set_xlabel("time on board 0's clock (us)")
            ax.set_ylabel("|rx|")
            ax.legend()
            ax.grid(True)
            fig.tight_layout()
            fig.savefig(args.plot, dpi=120)
            print(f"\nPlot saved to {args.plot}")
        if args.save:
            np.savez(args.save, **{f"board{b}_rx{ch}": p["data"] for (b, ch), p in plan.items()})
            print(f"Data saved to {args.save}")

        print()
        for msg in problems:
            print(f"MISMATCH: {msg}")
        for msg in trigger_problems:
            print(f"TRIGGER PROBLEM: {msg}")
        if trigger_problems:
            return 2
        if problems:
            return 1
        print("PASS: every selected stream came back from the right board and channel, nothing else did"
              + (", and each follower waited for board 0's trigger." if followers else "."))
        return 0
    finally:
        if multi is not None:
            scripts._close_board_connections(multi)


# ---------------------------------------------------------------- main

def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--select", nargs="+", metavar="BOARD:CHANNELS",
                   help="boards and RX channels to use, e.g. 0:0,1 1:1 (default: MARCOS_BOARDS)")
    p.add_argument("--ip", nargs="+", default=[], metavar="BOARD=ADDRESS[:PORT]",
                   help="override or add a board address, e.g. 1=169.254.93.61")
    p.add_argument("--trigger", nargs=2, type=float, metavar=("DELAY_US", "LENGTH_US"),
                   help="trigger delay and length (default: MARCOS_TRIGGER)")
    align = p.add_mutually_exclusive_group()
    align.add_argument("--align", dest="align", action="store_true", default=None,
                       help="line follower windows up with board 0 (default: MARCOS_ALIGN_FOLLOWERS)")
    align.add_argument("--no-align", dest="align", action="store_false", help="keep raw follower timing")
    p.add_argument("--loopback", action="store_true", help="send a TX0 pulse from board 0 (see above)")
    p.add_argument("--tx-amp", type=float, default=0.05, help="loopback pulse amplitude, 0-1 (default 0.05)")
    p.add_argument("--lo", type=float, help="LO frequency in MHz (default: the configured Larmor frequency)")
    p.add_argument("--rx-t", type=float, default=3.125, help="sample period in us (default 3.125)")
    p.add_argument("--samples", type=int, default=200,
                   help="window length unit; stream k gets k x this many samples (default 200)")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="seconds allowed beyond the sequence length before a follower counts as untriggered")
    p.add_argument("--connect-timeout", type=float, default=None,
                   help="seconds allowed to connect to all boards (default: MARCOS_CONNECT_TIMEOUT_S)")
    p.add_argument("--restart-hung", action="store_true",
                   help="restart marcos_server over SSH on a board that is stuck waiting (no bitstream reload)")
    p.add_argument("--repeat", type=int, default=1, help="repeat the test N times (catches intermittent triggers)")
    p.add_argument("--plot", metavar="PNG", help="save a plot of the received magnitudes")
    p.add_argument("--save", metavar="NPZ", help="save the received data")
    p.add_argument("--mri4all-base", metavar="FOLDER",
                   help="console data folder (Settings > Imaging Console > Study data); default: MRI4ALL_BASE")
    p.add_argument("--verbose", action="store_true", help="show MultiExperiment's own printout")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    base = resolve_base(args.mri4all_base)
    os.environ["MRI4ALL_BASE"] = str(base)
    os.environ.setdefault("MPLBACKEND", "Agg")
    import_console()
    marcos_cfg.apply_scanner_settings()

    boards = build_boards(args)
    trig = tuple(args.trigger) if args.trigger else tuple(cfg.MARCOS_TRIGGER)
    align = getattr(cfg, "MARCOS_ALIGN_FOLLOWERS", False) if args.align is None else args.align
    if args.lo is None:
        args.lo = float(cfg.LARMOR_FREQ)
    if args.connect_timeout is None:
        args.connect_timeout = getattr(cfg, "MARCOS_CONNECT_TIMEOUT_S", None) or 20.0

    print(f"Console config folder: {base}")
    print(f"FPGA clock {marcos_cfg.fpga_clk_freq_MHz} MHz, LO {args.lo} MHz, sample period {args.rx_t} us, "
          f"trigger delay {trig[0]:g} us / length {trig[1]:g} us, follower alignment {'on' if align else 'off'}")
    for b, s in boards.items():
        role = "controller (sends trigger)" if b == 0 else "follower (waits for trigger)"
        chans = ", ".join(f"RX{c}" for c in s["rx_channels"]) or "no receive"
        print(f"  board {b}: {s['ip']}:{s.get('port', 11111)}  {role}  {chans}")
    if args.loopback:
        print("  Loopback: board 0 will transmit a short TX0 pulse. TX0 must go to the RX inputs "
              "through an attenuator/splitter, not to the RF amplifier or coil.")

    print("\nChecking that each board's marcos_server answers:")
    if not preflight(boards, 3.0, args.restart_hung):
        return 2

    worst = 0
    args.repeat = max(1, args.repeat)
    for attempt in range(1, args.repeat + 1):
        code = run_once(boards, args, trig, align, attempt)
        worst = max(worst, code)
        if code == 2:
            break
    if args.repeat > 1:
        print(f"\n{attempt} run(s), worst result: " + {0: "PASS", 1: "selection mismatch",
                                                      2: "trigger/connection problem"}[worst])
    return worst


if __name__ == "__main__":
    sys.exit(main())
