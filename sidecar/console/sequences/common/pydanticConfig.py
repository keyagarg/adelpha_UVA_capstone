from pydantic import BaseModel, Field, model_validator
from typing import List, Literal
from pathlib import Path
from common import runtime

### rf_parameters section
class RfParameters(BaseModel):
    larmor_frequency_MHz: float = 15.52
    rf_maximum_amplitude_Hze: float = 10000 #7661.29
    rf_pi2_fraction: float = 0.6744

### gradients_parameters section
class GradientsParameters(BaseModel):
    gx_maximum: float = 8000000.0
    gy_maximum: float = 9000000.0
    gz_maximum: float = 10000000.0

### shim_parameters section
class ShimParameters(BaseModel):
    shim_x: float = 0.0
    shim_y: float = 0.0
    shim_z: float = 0.0
    shim_mc: List[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

### marcos_parameters.boards: one entry per Red Pitaya, set in Settings > Imaging Console.
### The list index is the board id. Board 0 is the controller: it uses the Scanner IP and
### the MaRCoS port, plays the RF pulse and gradients, and triggers the other boards.
### Only FID records on more than one board; other sequences use board 0 alone.
RxRole = Literal["off", "mri", "emi"]


class BoardParameters(BaseModel):
    ip: str = ""  # boards 1+; board 0 uses the Scanner IP
    port: int = 11111  # boards 1+; board 0 uses marcos_parameters.port
    # "mri": recorded by scans; "emi": recorded by the EMI tool; "off": not recorded
    rx0: RxRole = "off"
    rx1: RxRole = "off"
    # Board 0 only: the TX outputs that play the RF pulse
    tx0: bool = False
    tx1: bool = False
    # Program the receive filter's gain shift (marcos_client set_cic_shift)
    set_cic_shift: bool = False


def default_boards() -> List[BoardParameters]:
    """The original console: one board, RX0 for scans, TX0 for the RF pulse."""
    return [BoardParameters(rx0="mri", tx0=True)]


### marcos_parameters section
class MarcosParameters(BaseModel):
    port: int = 11111
    fpga_clock_frequency_MHz: float = 122.88
    gradient_board_type: str = "gpa-fhdo"
    gpa_fhdo_current_per_volt: float = 2.5
    flocra_pulseq_path: str = "./external/flocra_pulseq"
    # GPA-FHDO SPI setup on connect. Off by default: without a gradient board
    # attached, those first MaRCoS commands close the TCP socket (Broken pipe).
    initialize_gpa: bool = False
    boards: List[BoardParameters] = Field(default_factory=default_boards)

    @model_validator(mode="after")
    def _board0_transmits(self):
        # An empty list means the original single board, and board 0 always has a
        # TX output: without one a scan would play no RF pulse.
        if not self.boards:
            self.boards = default_boards()
        if not (self.boards[0].tx0 or self.boards[0].tx1):
            self.boards[0].tx0 = True
        return self

##############################################
### main definition of the configuration files

path = Path(runtime.get_base_path()) / "config/config_acq.json"

class Config(BaseModel):
    rf_parameters: RfParameters
    gradients_parameters: GradientsParameters
    shim_parameters: ShimParameters
    marcos_parameters: MarcosParameters

def configCreator():
    config_data = Config(rf_parameters={}, gradients_parameters={}, shim_parameters={}, marcos_parameters={})
    config_data.rf_parameters = RfParameters()
    config_data.gradients_parameters = GradientsParameters()
    config_data.shim_parameters = ShimParameters()
    config_data.marcos_parameters = MarcosParameters()
    return config_data

