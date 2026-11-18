from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from common.types import (
    ExamInformation,
    PatientInformation,
    ScanQueueEntry,
    ScanTask,
    SystemInformation,
)


class ExamStartRequest(BaseModel):
    patient: PatientInformation
    acc: str = ""
    patient_position: str = "HFS"


class ExamResponse(BaseModel):
    exam: ExamInformation
    patient: PatientInformation
    system: SystemInformation


class ScanCreateRequest(BaseModel):
    sequence: str
    protocol_name: str = ""
    prepared: bool = False


class ScanUpdateRequest(BaseModel):
    parameters: Optional[Dict[str, Any]] = None
    protocol_name: Optional[str] = None
    other: Optional[Dict[str, Any]] = None
    processing: Optional[Dict[str, Any]] = None


class ScanValidateRequest(BaseModel):
    parameters: Dict[str, Any] = Field(default_factory=dict)


class ValidateResponse(BaseModel):
    ok: bool
    problems: List[str] = Field(default_factory=list)


class SeqFileUploadResponse(BaseModel):
    name: str


class SequenceInfo(BaseModel):
    id: str
    name: str
    description: str = ""
    adjustment: bool = False
    defaults: Dict[str, Any] = Field(default_factory=dict)
    parameter_schema: Dict[str, Any] = Field(default_factory=dict)


class DevicePingResponse(BaseModel):
    ip: str
    ok: bool
    simulation: bool = False
    reachable: bool = False
    method: str = ""
    detail: str = ""


class DeviceMarcosStartResponse(BaseModel):
    ok: bool
    started: bool = False
    compiled: bool = False
    bitstream: bool = False
    detail: str = ""
    ip: str = ""


class ServiceStatusResponse(BaseModel):
    acq: Optional[bool] = None
    recon: Optional[bool] = None
    mode: str = "unknown"
    last_error: str = ""
    sequence_registry: bool = False


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "mri4all-api"
    exam_active: bool = False
    sequences: int = 0
    hardware_simulation: bool = False
    sequence_registry: bool = False
    pipeline: bool = False


class EventRespondRequest(BaseModel):
    response: Any = None
    error: bool = False
    source: str = "acq"


class ScanDetail(BaseModel):
    entry: ScanQueueEntry
    task: Optional[ScanTask] = None
    folder: str = ""
    editing: bool = False
    prepared: bool = False


class ScanPsdRequest(BaseModel):
    parameters: Optional[Dict[str, Any]] = None


class ScanPsdResponse(BaseModel):
    folder: str
    file_path: str = "other/psd.plot"
    result_type: str = "plot"
    result_name: str = "PSD"


class EmiMeasureRequest(BaseModel):
    """Settings for one EMI noise burst. Every field is optional."""

    n_samples: int = Field(default=4096, ge=64, le=65536)
    adc_duration_us: float = Field(default=6400.0, gt=0, le=1_000_000)
    averages: int = Field(default=1, ge=1, le=64)
    # Also record board 0 RX0 (the MRI channel) as a reference trace.
    include_reference: bool = False


class EmiChannel(BaseModel):
    board: int
    channel: str
    label: str
    role: str


class EmiTrace(EmiChannel):
    magnitude: List[float]
    time_magnitude: List[float]
    rms: float
    peak_frequency_hz: float
    peak_magnitude: float


class EmiCapture(BaseModel):
    capture_id: str
    timestamp: str
    source: str
    center_frequency_hz: float
    sample_period_s: float
    bandwidth_hz: float
    n_samples: int
    averages: int
    units: str
    frequency_hz: List[float]
    traces: List[EmiTrace]


class EmiSaveRequest(BaseModel):
    capture_id: str


class EmiSaveResponse(BaseModel):
    capture_id: str
    folder: str
    files: List[str]


class EmiStatus(BaseModel):
    available: bool
    busy: bool
    simulation: bool
    channels: List[EmiChannel]
    reason: str = ""