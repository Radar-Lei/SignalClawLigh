from typing import Protocol
from signalclaw.core.state import NetworkObservation, CyclePlan, PhaseCommand

class CyclePlannerSkill(Protocol):
    def plan(self, obs: NetworkObservation) -> CyclePlan:
        ...

class PhaseMicroSkill(Protocol):
    def decide(self, obs: NetworkObservation, plan: CyclePlan) -> PhaseCommand:
        ...
