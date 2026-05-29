from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

@dataclass(frozen=True)
class PhaseObservation:
    """Single phase state at an intersection"""
    phase_id: int
    queue: float  # number of waiting vehicles
    waiting_time: float  # average waiting time in seconds
    predicted_arrival: float  # predicted arriving vehicles in next cycle
    elapsed_green: float  # seconds of green already given
    min_green: float  # minimum green time constraint
    max_green: float  # maximum green time constraint
    saturation_flow: float = 1900.0  # vehicles per hour per lane

@dataclass(frozen=True)
class IntersectionObservation:
    """Complete state of a single intersection"""
    crossing_id: str  # TLS id in SUMO
    current_phase_id: int
    current_phase_elapsed: float  # seconds elapsed in current phase
    cycle_second: float  # total cycle length (0 if cycle-free)
    phases: Dict[int, PhaseObservation]
    downstream_queue: Dict[str, float]  # downstream edge -> queue
    upstream_queue: Dict[str, float]  # upstream edge -> queue
    downstream_spillback_risk: float = 0.0
    upstream_release_pressure: float = 0.0

@dataclass(frozen=True)
class NetworkObservation:
    """Network state visible to a single intersection"""
    ego: IntersectionObservation
    neighbors: Dict[str, IntersectionObservation]  # neighbor tls_id -> observation
    timestamp: float = 0.0

@dataclass(frozen=True)
class CyclePlan:
    """Output of CyclePlannerSkill"""
    cycle_length: float
    green_times: Dict[int, float]  # phase_id -> green seconds
    phase_order: List[int]
    offset_target: Optional[float] = None

@dataclass(frozen=True)
class PhaseCommand:
    """Output of PhaseMicroSkill"""
    action: str  # "hold" | "switch" | "extend" | "shorten"
    next_phase_id: int
    duration: float  # seconds
    reason_code: str = ""
