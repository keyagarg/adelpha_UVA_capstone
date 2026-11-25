from sequences.common.util import reading_json_parameter

# Extracting configuration
# TODO: This should be reworked
configuration_data = reading_json_parameter()

# System Larmor Frequency, in MHz
LARMOR_FREQ = configuration_data.rf_parameters.larmor_frequency_MHz
# System maximum RF amplitude, in Hz - 1e4
RF_MAX = configuration_data.rf_parameters.rf_maximum_amplitude_Hze
# Fraction of power to expect a pi/2 pulse
RF_PI2_FRACTION = configuration_data.rf_parameters.rf_pi2_fraction
# System maximum X gradient strength, in Hz/m
GX_MAX = configuration_data.gradients_parameters.gx_maximum
# System maximum Y gradient strength, in Hz/m
GY_MAX = configuration_data.gradients_parameters.gy_maximum
# System maximum Z gradient strength, in Hz/m
GZ_MAX = configuration_data.gradients_parameters.gz_maximum

SHIM_X = configuration_data.shim_parameters.shim_x
SHIM_Y = configuration_data.shim_parameters.shim_y
SHIM_Z = configuration_data.shim_parameters.shim_z
SHIM_MC = configuration_data.shim_parameters.shim_mc

DBG_FA_EXC = 90
DBG_FA_REF = 180

# Red Pitaya boards come from Settings > Imaging Console > MaRCoS boards, saved in
# config_acq.json as marcos_parameters.boards. Board 0 is the controller and uses the
# Scanner IP. The values below are rebuilt by update(); board_layout() describes them.
MARCOS_BOARDS = None  # FID's boards, or None for the original single-board path
MARCOS_BOARD_ADDRESSES = {}  # every board with an address: {id: dict(ip, port[, set_cic_shift])}
MARCOS_RX_ROLES = {}  # {(board, channel): "mri" or "emi"}
MARCOS_TX_CHANNELS = (0,)  # board 0 TX outputs that play the RF pulse
MARCOS_SINGLE_BOARD_RX = 0  # board 0 receiver used by single-board sequences, or None
MARCOS_BOARD_PROBLEMS = []  # board settings that could not be used, and why

# Trigger delay and length (microseconds), matching the MultiExperiment example.
MARCOS_TRIGGER = (10, 20000)
# A follower starts its program when board 0's trigger arrives, then waits its own
# initial_wait (17 us with the console's settings) before its first event, so its
# samples lag board 0's by trig_delay + initial_wait. True moves follower receive
# windows earlier by that amount so samples line up with board 0's (to within the
# trigger's hardware latency). False keeps the raw timing.
MARCOS_ALIGN_FOLLOWERS = True
# Seconds allowed for connecting to every board. A marcos_server still stuck in an
# earlier scan accepts the connection but never answers. None waits forever.
MARCOS_CONNECT_TIMEOUT_S = 20
# Seconds allowed beyond the sequence's own length (plus the trigger pulse) before a
# multi-board run is abandoned, e.g. when a follower never sees the trigger.
# None waits forever.
MARCOS_RUN_TIMEOUT_S = 15


def marcos_board_endpoints(boards):
    """Use the same explicit board addresses for acquisition and preflight."""
    if not boards or 0 not in boards:
        raise ValueError("boards must include board 0, the primary")
    endpoints = {}
    for board_id, settings in boards.items():
        if type(board_id) is not int or board_id < 0:
            raise ValueError("Board IDs must be nonnegative integers")
        ip = settings.get("ip")
        port = settings.get("port", 11111)
        if not isinstance(ip, str) or not ip.strip():
            raise ValueError(f"Board {board_id} needs an IP address or hostname")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"Invalid MaRCoS port for board {board_id}")
        endpoints[board_id] = (ip.strip(), port)
    if len(set(endpoints.values())) != len(endpoints):
        raise ValueError("Configured boards must use distinct MaRCoS endpoints")
    return endpoints


def _scanner_ip():
    """Board 0's address: the Scanner IP in Settings > Imaging Console (mri4all.json)."""
    try:
        from common.config import Configuration

        return Configuration.load_from_file().scanner_ip.strip()
    except Exception:
        return ""


def board_layout(boards, scanner_ip, port):
    """Turn the board settings into the values the console uses.

    ``boards`` is ``marcos_parameters.boards``; the list index is the board id.
    Board 0 takes ``scanner_ip`` and ``port``; other boards their own address.

    MARCOS_BOARDS: FID's boards, ``{id: dict(ip, port, rx_channels[, set_cic_shift])}``
        with each board's MRI receivers. Board 0 is always included because it sends
        the trigger. None when the only MRI receiver is board 0 RX0, which keeps the
        original single-board path.
    MARCOS_BOARD_ADDRESSES: every board with an address, for EMI and the board test.
    MARCOS_RX_ROLES: ``{(board, channel): "mri" or "emi"}``; receivers set to off are left out.
    MARCOS_TX_CHANNELS: board 0's TX outputs for the RF pulse.
    MARCOS_SINGLE_BOARD_RX: board 0's first MRI receiver, used by sequences that record
        on one channel; None when board 0 has none.
    MARCOS_BOARD_PROBLEMS: settings that could not be used, as sentences.
    """
    addresses, roles, problems = {}, {}, []
    for board_id, board in enumerate(boards):
        ip = scanner_ip if board_id == 0 else (board.ip or "").strip()
        if not ip:
            problems.append("Board 0 needs a Scanner IP." if board_id == 0
                            else f"Board {board_id} has no address, so it is left out.")
            continue
        address = dict(ip=ip, port=int(port if board_id == 0 else board.port))
        if board.set_cic_shift:
            address["set_cic_shift"] = True
        addresses[board_id] = address
        for channel in (0, 1):
            role = getattr(board, f"rx{channel}")
            if role != "off":
                roles[(board_id, channel)] = role
    tx_channels = tuple(ch for ch in (0, 1) if boards and getattr(boards[0], f"tx{ch}")) or (0,)
    mri = {}
    for (board_id, channel), role in sorted(roles.items()):
        if role == "mri":
            mri.setdefault(board_id, []).append(channel)
    if not any(getattr(board, f"rx{ch}") == "mri" for board in boards for ch in (0, 1)):
        problems.append("No receiver is marked MRI, so scans have nothing to record.")
    if 0 not in addresses or not mri or mri == {0: [0]}:
        marcos_boards = None
    else:
        marcos_boards = {
            board_id: {**addresses[board_id], "rx_channels": tuple(mri.get(board_id, ()))}
            for board_id in sorted({0, *mri})
        }
    return dict(
        MARCOS_BOARDS=marcos_boards,
        MARCOS_BOARD_ADDRESSES=addresses,
        MARCOS_RX_ROLES=roles,
        MARCOS_TX_CHANNELS=tx_channels,
        MARCOS_SINGLE_BOARD_RX=mri[0][0] if mri.get(0) else None,
        MARCOS_BOARD_PROBLEMS=problems,
    )


def _apply_board_layout(data):
    layout = board_layout(data.marcos_parameters.boards, _scanner_ip(), data.marcos_parameters.port)
    globals().update(layout)


_apply_board_layout(configuration_data)


def update():
    global configuration_data
    global LARMOR_FREQ, RF_MAX, RF_PI2_FRACTION, GX_MAX, GY_MAX, GZ_MAX, SHIM_X, SHIM_Y, SHIM_Z, SHIM_MC

    configuration_data = reading_json_parameter()
    LARMOR_FREQ = configuration_data.rf_parameters.larmor_frequency_MHz
    RF_MAX = configuration_data.rf_parameters.rf_maximum_amplitude_Hze
    RF_PI2_FRACTION = configuration_data.rf_parameters.rf_pi2_fraction
    GX_MAX = configuration_data.gradients_parameters.gx_maximum
    GY_MAX = configuration_data.gradients_parameters.gy_maximum
    GZ_MAX = configuration_data.gradients_parameters.gz_maximum
    SHIM_X = configuration_data.shim_parameters.shim_x
    SHIM_Y = configuration_data.shim_parameters.shim_y
    SHIM_Z = configuration_data.shim_parameters.shim_z
    SHIM_MC = configuration_data.shim_parameters.shim_mc
    _apply_board_layout(configuration_data)


# TODO: Needs to be revised
MGH_PATH = "/tmp"
LOG_PATH = "./server/log/"
SEQ_PATH = "./server/scanner_control/seq/"
DATA_PATH = "../data/scanner_control/data/"


# LARMOR_FREQ = 15.52 # System Larmor Frequency, in MHz
# RF_MAX = 7661.29 # System maximum RF amplitude, in Hz - 1e4
# RF_PI2_FRACTION =0.6744 # Fraction of power to expect a pi/2 pulse

# GX_MAX = 8.0e6 # System maximum X gradient strength, in Hz/m
# GY_MAX = 9.2e6 # System maximum Y gradient strength, in Hz/m
# GZ_MAX = 10e6 # System maximum Z gradient strength, in Hz/m

# SHIM_X = 0
# SHIM_Y = 0
# SHIM_Z = 0
