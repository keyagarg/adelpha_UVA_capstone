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

# Optional board selection for FID. None keeps the original single-board RX0 path.
# To use both receivers on the primary:
# MARCOS_BOARDS = {0: dict(ip="169.254.44.172", port=11111, rx_channels=(0, 1))}
# To also use the secondary, add its actual address to that dictionary:
#     1: dict(ip="SECOND_BOARD_IP", port=11111)
# Secondary receivers default to (0, 1); use rx_channels=(1,) for RX1 only.
# This is a Python setting; it is not read from config_acq.json or the GUI.
MARCOS_BOARDS = None
# Trigger delay and length (microseconds), matching the MultiExperiment example.
MARCOS_TRIGGER = (10, 20000)


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
