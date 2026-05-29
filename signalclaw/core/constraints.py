from dataclasses import dataclass
from typing import Dict

@dataclass
class IntersectionConstraints:
    min_green: float = 10.0
    max_green: float = 60.0
    min_cycle: float = 40.0
    max_cycle: float = 180.0
    yellow_time: float = 3.0
    all_red_time: float = 2.0
    max_extend: float = 5.0
    max_shorten: float = 5.0
    force_phase_ids: set = None  # phases that must appear every cycle

    def __post_init__(self):
        if self.force_phase_ids is None:
            self.force_phase_ids = set()

@dataclass
class NetworkConstraints:
    intersections: Dict[str, IntersectionConstraints]  # tls_id -> constraints

    def get(self, tls_id: str) -> IntersectionConstraints:
        return self.intersections.get(tls_id, IntersectionConstraints())
