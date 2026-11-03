"""Model result contract (spec section 7).

The model worker delivers ModelResult records without blocking the scan
cycle. This module decides whether the latest result may be used for
treatment; it never authorises outputs by itself.
"""

from dataclasses import dataclass
from enum import Enum
from pasteurizer.numbers import is_finite_number


class ModelStatus(str, Enum):
    TRACKING = "TRACKING"
    ADAPTING = "ADAPTING"
    CHECKING = "CHECKING"
    UNRELIABLE = "UNRELIABLE"
    UNAVAILABLE = "UNAVAILABLE"


class InputSource(str, Enum):
    LIVE = "LIVE"
    SYNTHETIC = "SYNTHETIC"


@dataclass(frozen=True)
class ModelResult:
    sequence: int
    computed_at: float           # monotonic
    input_start: float           # monotonic, first input covered
    input_end: float             # monotonic, latest input covered
    input_source: InputSource
    status: ModelStatus
    history_valid: bool
    predicted_log_reduction: float | None   # weakest relevant branch/parcel
    achievable_flow_l_min: float | None     # flow meeting the operating target
    entry_target_met: bool
    predicted_pt3_c: float | None
    residual_c: float | None                # predicted - time-aligned measured PT3
    parameter_version: str = ""


class ModelSupervisor:
    """Keeps the latest result and judges it against the spec's usability rule."""

    def __init__(self, max_result_age_s: float):
        self.max_age = max_result_age_s
        self.latest: ModelResult | None = None
        self.rejected = 0

    def submit(self, result: ModelResult) -> bool:
        if not isinstance(result, ModelResult):
            raise TypeError("Expected ModelResult")
        if self.latest is not None and (
            result.sequence <= self.latest.sequence
            or result.input_end < self.latest.input_end
        ):
            self.rejected += 1
            return False
        self.latest = result
        return True

    def usable(self, now: float, enabled: bool) -> tuple[bool, str]:
        r = self.latest
        if not enabled:
            return False, "Model control disabled"
        if r is None:
            return False, "No model result"
        if r.input_source != InputSource.LIVE:
            return False, "Synthetic model input"
        if r.status != ModelStatus.TRACKING:
            return False, f"Model status {r.status.value}"
        if not r.history_valid:
            return False, "Parcel history invalid"
        if now - r.input_end > self.max_age:
            return False, "Model result too old"
        if not (is_finite_number(r.predicted_log_reduction) and is_finite_number(r.achievable_flow_l_min)):
            return False, "Model result incomplete"
        return True, "Model usable"

    @property
    def unreliable(self) -> bool:
        return self.latest is not None and self.latest.status == ModelStatus.UNRELIABLE
