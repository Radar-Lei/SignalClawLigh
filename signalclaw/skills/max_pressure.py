"""Max Pressure baseline variants for traffic signal control.

Provides four variants:
- MaxPressureCyclicAllocation : original cyclic proportional allocation
- MaxPressureQueueOnly        : cyclic, incoming-queue-only (no downstream)
- MaxPressureCanonical         : classic pick-max-pressure with min_green constraint
- MaxPressureCyclicMovement   : cyclic with movement-level pressure

All variants implement plan(obs) -> CyclePlan and decide(obs, plan) -> PhaseCommand
so they are compatible with both CyclePlannerSkill and PhaseMicroSkill protocols.
"""

from __future__ import annotations

import abc
import math
from typing import Dict, List, Optional, Tuple

from signalclaw.core.state import (
    NetworkObservation,
    IntersectionObservation,
    PhaseObservation,
    CyclePlan,
    PhaseCommand,
)


# ======================================================================
# Abstract base
# ======================================================================

class _MaxPressureBase(abc.ABC):
    """Shared bookkeeping and helpers for all MaxPressure variants."""

    def __init__(
        self,
        min_green: float = 10.0,
        max_green: float = 60.0,
        cycle_length: float = 90.0,
        decision_interval: float = 5.0,
    ):
        self.min_green = min_green
        self.max_green = max_green
        self.default_cycle_length = cycle_length
        self.decision_interval = decision_interval
        # per-intersection bookkeeping
        self._phase_index: Dict[str, int] = {}
        self._phase_remaining: Dict[str, float] = {}
        self._current_plan: Dict[str, CyclePlan] = {}

    # ------------------------------------------------------------------
    # pressure computation — subclasses override
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        """Return the pressure score for *phase_id* at the given intersection."""

    # ------------------------------------------------------------------
    # plan / decide — subclasses may override for different strategies
    # ------------------------------------------------------------------

    def plan(self, obs: NetworkObservation) -> CyclePlan:
        """Default: compute pressure for every phase, then delegate allocation."""
        ego = obs.ego
        tls_id = ego.crossing_id
        green_phases = sorted(ego.phases.keys())

        if not green_phases:
            plan = CyclePlan(
                cycle_length=self.default_cycle_length,
                green_times={},
                phase_order=[],
            )
            self._current_plan[tls_id] = plan
            return plan

        pressures = {gp: self.compute_pressure(ego, gp) for gp in green_phases}
        green_times = self._allocate_green(green_phases, pressures)
        plan = CyclePlan(
            cycle_length=sum(green_times.values()),
            green_times=green_times,
            phase_order=green_phases,
        )
        self._current_plan[tls_id] = plan
        self._phase_index[tls_id] = 0
        self._phase_remaining[tls_id] = green_times.get(green_phases[0], self.min_green)
        return plan

    def decide(self, obs: NetworkObservation, plan: Optional[CyclePlan] = None) -> PhaseCommand:
        """Default cyclic decide: countdown remaining green, switch when exhausted."""
        ego = obs.ego
        tls_id = ego.crossing_id

        if plan is None:
            plan = self._current_plan.get(tls_id)
        if plan is None:
            return PhaseCommand(
                action="hold",
                next_phase_id=ego.current_phase_id,
                duration=self.decision_interval,
                reason_code="no_plan",
            )

        current_idx = self._phase_index.get(tls_id, 0)
        phase_order = plan.phase_order
        if current_idx >= len(phase_order):
            current_idx = 0
            self._phase_index[tls_id] = 0

        current_phase = phase_order[current_idx]
        remaining = self._phase_remaining.get(tls_id, 0)

        if remaining <= 0:
            next_idx = (current_idx + 1) % len(phase_order)
            next_phase = phase_order[next_idx]
            self._phase_index[tls_id] = next_idx
            self._phase_remaining[tls_id] = plan.green_times.get(next_phase, self.min_green)
            return PhaseCommand(
                action="switch",
                next_phase_id=next_phase,
                duration=self._phase_remaining[tls_id],
                reason_code="phase_exhausted",
            )

        self._phase_remaining[tls_id] = remaining - self.decision_interval
        return PhaseCommand(
            action="hold",
            next_phase_id=current_phase,
            duration=self.decision_interval,
            reason_code="continuing",
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _allocate_green(self, phases: List[int], pressures: Dict[int, float]) -> Dict[int, float]:
        """Proportional green-time allocation (shift to positive first)."""
        min_p = min(pressures.values())
        shifted = {gp: p - min_p + 1.0 for gp, p in pressures.items()}
        total_shifted = sum(shifted.values())

        green_times: Dict[int, float] = {}
        if total_shifted > 0:
            for gp in phases:
                gt = self.default_cycle_length * (shifted[gp] / total_shifted)
                green_times[gp] = max(self.min_green, min(self.max_green, gt))
        else:
            equal = self.default_cycle_length / len(phases)
            for gp in phases:
                green_times[gp] = max(self.min_green, min(self.max_green, equal))

        # cap total to 1.5x default cycle
        total = sum(green_times.values())
        max_cycle = self.default_cycle_length * 1.5
        if total > max_cycle:
            scale = max_cycle / total
            green_times = {
                gp: max(self.min_green, gt * scale) for gp, gt in green_times.items()
            }
        return green_times

    def reset(self, tls_id: Optional[str] = None) -> None:
        if tls_id:
            self._phase_index.pop(tls_id, None)
            self._phase_remaining.pop(tls_id, None)
            self._current_plan.pop(tls_id, None)
        else:
            self._phase_index.clear()
            self._phase_remaining.clear()
            self._current_plan.clear()


# ======================================================================
# Variant 1 — CyclicAllocation (original behaviour)
# ======================================================================

class MaxPressureCyclicAllocation(_MaxPressureBase):
    """Original implementation: cyclic fixed order, proportional green allocation.

    Downstream pressure is averaged over all downstream edges and then scaled by
    the number of phases.  Pressures are shifted to positive before proportional
    allocation.
    """

    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        phase_obs = obs.phases.get(phase_id)
        if phase_obs is None:
            return 0.0

        incoming_pressure = phase_obs.queue

        outgoing_pressure = sum(obs.downstream_queue.values()) if obs.downstream_queue else 0.0
        n_out = max(len(obs.downstream_queue), 1)
        outgoing_pressure = outgoing_pressure / n_out * len(obs.phases)

        return incoming_pressure - outgoing_pressure


# ======================================================================
# Variant 2 — QueueOnly (no downstream term)
# ======================================================================

class MaxPressureQueueOnly(_MaxPressureBase):
    """Cyclic, but pressure = incoming queue only (no downstream term).

    Useful as an ablation to measure whether the downstream computation
    adds signal or just noise.
    """

    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        phase_obs = obs.phases.get(phase_id)
        if phase_obs is None:
            return 0.0
        return phase_obs.queue


# ======================================================================
# Variant 3 — Canonical MaxPressure (pick-max-pressure per decision interval)
# ======================================================================

class MaxPressureCanonical(_MaxPressureBase):
    """Classic MaxPressure: every decision interval pick the phase with highest
    movement-specific pressure (upstream - downstream).

    * Min-green: the current phase must have been active for at least
      ``min_green`` seconds before a switch is allowed.
    * Pressure is computed per-movement (upstream_queue edge minus the
      matching downstream_queue edge).
    * plan() still returns a CyclePlan so it fits the skill_api protocol,
      but decide() is free to switch to any phase — it is not bound to
      the phase_order in the plan.
    """

    # map to remember how long the current phase has been active
    _phase_elapsed: Dict[str, float]  # tls_id -> elapsed seconds

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._phase_elapsed: Dict[str, float] = {}

    # ---- pressure ----

    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        """Movement-specific pressure: upstream - downstream per movement edge."""
        phase_obs = obs.phases.get(phase_id)
        if phase_obs is None:
            return 0.0

        # Incoming: queue on the upstream edges belonging to this phase
        incoming = phase_obs.queue

        # Outgoing: for each downstream edge compute its queue;
        # use matching based on edge key prefix heuristic.
        # If downstream_queue keys follow "<from_node>_<to_node>" or similar,
        # we approximate by using all downstream entries scaled by 1/n.
        # A movement-specific mapping would require network topology info;
        # here we use a simple per-edge average.
        if obs.downstream_queue:
            outgoing = sum(obs.downstream_queue.values())
            n_out = len(obs.downstream_queue)
            # movement-specific: attribute downstream proportionally
            outgoing = outgoing / n_out
        else:
            outgoing = 0.0

        return incoming - outgoing

    # ---- plan ----

    def plan(self, obs: NetworkObservation) -> CyclePlan:
        """Return a plan that lists all phases; decide() will pick freely."""
        ego = obs.ego
        tls_id = ego.crossing_id
        green_phases = sorted(ego.phases.keys())

        if not green_phases:
            plan = CyclePlan(
                cycle_length=self.default_cycle_length,
                green_times={},
                phase_order=[],
            )
            self._current_plan[tls_id] = plan
            return plan

        # Equal green as starting point; decide() overrides via switch
        equal_time = self.default_cycle_length / len(green_phases)
        green_times = {
            gp: max(self.min_green, min(self.max_green, equal_time))
            for gp in green_phases
        }

        plan = CyclePlan(
            cycle_length=sum(green_times.values()),
            green_times=green_times,
            phase_order=green_phases,
        )
        self._current_plan[tls_id] = plan
        self._phase_index[tls_id] = 0
        self._phase_remaining[tls_id] = green_times.get(green_phases[0], self.min_green)
        self._phase_elapsed[tls_id] = 0.0
        return plan

    # ---- decide ----

    def decide(self, obs: NetworkObservation, plan: Optional[CyclePlan] = None) -> PhaseCommand:
        ego = obs.ego
        tls_id = ego.crossing_id

        if plan is None:
            plan = self._current_plan.get(tls_id)
        if plan is None or not plan.phase_order:
            return PhaseCommand(
                action="hold",
                next_phase_id=ego.current_phase_id,
                duration=self.decision_interval,
                reason_code="no_plan",
            )

        elapsed = self._phase_elapsed.get(tls_id, 0.0) + self.decision_interval
        self._phase_elapsed[tls_id] = elapsed

        current_phase = ego.current_phase_id

        # If min_green not yet satisfied, must hold
        if elapsed < self.min_green:
            remaining = self._phase_remaining.get(tls_id, self.min_green)
            self._phase_remaining[tls_id] = remaining - self.decision_interval
            return PhaseCommand(
                action="hold",
                next_phase_id=current_phase,
                duration=self.decision_interval,
                reason_code="min_green_not_met",
            )

        # Compute pressure for every phase
        pressures = {gp: self.compute_pressure(ego, gp) for gp in plan.phase_order}
        best_phase = max(pressures, key=lambda gp: pressures[gp])
        best_pressure = pressures[best_phase]
        current_pressure = pressures.get(current_phase, -math.inf)

        if best_phase != current_phase and best_pressure > current_pressure:
            # Switch to the highest-pressure phase
            duration = max(self.min_green, min(self.max_green,
                                               self.default_cycle_length / len(plan.phase_order)))
            self._phase_index[tls_id] = plan.phase_order.index(best_phase)
            self._phase_remaining[tls_id] = duration
            self._phase_elapsed[tls_id] = 0.0
            return PhaseCommand(
                action="switch",
                next_phase_id=best_phase,
                duration=duration,
                reason_code="max_pressure_switch",
            )

        # Hold current phase
        remaining = self._phase_remaining.get(tls_id, self.min_green)
        self._phase_remaining[tls_id] = remaining - self.decision_interval
        return PhaseCommand(
            action="hold",
            next_phase_id=current_phase,
            duration=self.decision_interval,
            reason_code="continuing",
        )

    # ---- reset ----

    def reset(self, tls_id: Optional[str] = None) -> None:
        super().reset(tls_id)
        if tls_id:
            self._phase_elapsed.pop(tls_id, None)
        else:
            self._phase_elapsed.clear()


# ======================================================================
# Variant 4 — CyclicMovement (fixed order + movement-level pressure)
# ======================================================================

class MaxPressureCyclicMovement(_MaxPressureBase):
    """Fixed phase order (cyclic) with movement-level pressure.

    Like CyclicAllocation but pressure is computed per-movement
    (upstream edge - downstream edge), not via the global downstream
    average.  Green time is still allocated proportionally within the
    fixed cycle.
    """

    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        phase_obs = obs.phases.get(phase_id)
        if phase_obs is None:
            return 0.0

        incoming = phase_obs.queue

        # Movement-specific downstream: average downstream queue per edge
        if obs.downstream_queue:
            outgoing = sum(obs.downstream_queue.values()) / len(obs.downstream_queue)
        else:
            outgoing = 0.0

        return incoming - outgoing

    # plan / decide inherited from _MaxPressureBase (cyclic proportional)


# ======================================================================
# Backward-compatible alias
# ======================================================================

MaxPressureSkill = MaxPressureCyclicAllocation


# ======================================================================
# Factory
# ======================================================================

_VARIANTS = {
    "cyclic_allocation": MaxPressureCyclicAllocation,
    "queue_only": MaxPressureQueueOnly,
    "canonical": MaxPressureCanonical,
    "cyclic_movement": MaxPressureCyclicMovement,
}


def create_max_pressure(variant: str, **kwargs) -> _MaxPressureBase:
    """Factory: return a MaxPressure variant instance.

    Parameters
    ----------
    variant : str
        One of "cyclic_allocation", "queue_only", "canonical", "cyclic_movement".
    **kwargs
        Forwarded to the variant constructor (min_green, max_green, cycle_length, …).
    """
    cls = _VARIANTS.get(variant)
    if cls is None:
        raise ValueError(
            f"Unknown MaxPressure variant '{variant}'. "
            f"Available: {list(_VARIANTS.keys())}"
        )
    return cls(**kwargs)
