import math
from typing import Dict, List, Optional
from signalclaw.core.state import (
    NetworkObservation, IntersectionObservation,
    CyclePlan, PhaseCommand
)


class MaxPressureSkill:
    """
    Max Pressure baseline: selects phases and allocates green time
    based on pressure (incoming queue - outgoing queue).

    Uses a cycle-based variant where all phases are served in order,
    but green time is allocated proportionally to pressure.
    """

    def __init__(self, min_green: float = 10.0, max_green: float = 60.0,
                 cycle_length: float = 90.0, decision_interval: float = 5.0):
        self.min_green = min_green
        self.max_green = max_green
        self.default_cycle_length = cycle_length
        self.decision_interval = decision_interval
        # Track phase state per intersection
        self._phase_index: Dict[str, int] = {}  # tls_id -> current phase index in order
        self._phase_remaining: Dict[str, float] = {}  # tls_id -> remaining green time
        self._current_plan: Dict[str, CyclePlan] = {}

    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        """Compute pressure for a specific phase at an intersection"""
        phase_obs = obs.phases.get(phase_id)
        if phase_obs is None:
            return 0.0

        # Pressure = incoming queue - outgoing queue
        # Incoming queue: vehicles waiting on edges feeding into this phase
        incoming_pressure = phase_obs.queue

        # Outgoing queue: downstream congestion
        # Use downstream_queue from the intersection observation
        # Sum all downstream queues (this is a simplification)
        outgoing_pressure = sum(obs.downstream_queue.values()) if obs.downstream_queue else 0.0

        # Normalize by number of outgoing edges
        n_out = max(len(obs.downstream_queue), 1)
        outgoing_pressure = outgoing_pressure / n_out * len(obs.phases)

        return incoming_pressure - outgoing_pressure

    def plan_cycle(self, obs: NetworkObservation) -> CyclePlan:
        """Plan a cycle based on pressure"""
        ego = obs.ego
        tls_id = ego.crossing_id

        # Get green phases in order
        green_phases = sorted(ego.phases.keys())
        if not green_phases:
            return CyclePlan(cycle_length=self.default_cycle_length, green_times={}, phase_order=[])

        # Compute pressure for each green phase
        pressures = {}
        for gp in green_phases:
            pressures[gp] = self.compute_pressure(ego, gp)

        # Allocate green time proportionally to pressure
        # Shift pressures to be positive for proportional allocation
        min_pressure = min(pressures.values())
        shifted = {gp: p - min_pressure + 1.0 for gp, p in pressures.items()}
        total_shifted = sum(shifted.values())

        green_times = {}
        if total_shifted > 0:
            for gp in green_phases:
                gt = self.default_cycle_length * (shifted[gp] / total_shifted)
                green_times[gp] = max(self.min_green, min(self.max_green, gt))
        else:
            # Equal allocation if all pressures are similar
            equal_time = self.default_cycle_length / len(green_phases)
            for gp in green_phases:
                green_times[gp] = max(self.min_green, min(self.max_green, equal_time))

        # Adjust to ensure total fits within reasonable cycle length
        total = sum(green_times.values())
        max_cycle = self.default_cycle_length * 1.5
        min_cycle = self.default_cycle_length * 0.5
        if total > max_cycle:
            scale = max_cycle / total
            green_times = {gp: max(self.min_green, gt * scale) for gp, gt in green_times.items()}

        cycle_length = sum(green_times.values())

        plan = CyclePlan(
            cycle_length=cycle_length,
            green_times=green_times,
            phase_order=green_phases,
        )
        self._current_plan[tls_id] = plan
        self._phase_index[tls_id] = 0
        self._phase_remaining[tls_id] = green_times.get(green_phases[0], self.min_green)

        return plan

    def decide(self, obs: NetworkObservation, plan: Optional[CyclePlan] = None) -> PhaseCommand:
        """Decide what to do at current step"""
        ego = obs.ego
        tls_id = ego.crossing_id

        if plan is None:
            plan = self._current_plan.get(tls_id)
        if plan is None:
            return PhaseCommand(
                action="hold", next_phase_id=ego.current_phase_id,
                duration=self.decision_interval, reason_code="no_plan"
            )

        # Get current phase in our order
        current_idx = self._phase_index.get(tls_id, 0)
        phase_order = plan.phase_order

        if current_idx >= len(phase_order):
            current_idx = 0
            self._phase_index[tls_id] = 0

        current_phase = phase_order[current_idx]
        remaining = self._phase_remaining.get(tls_id, 0)

        # Check if current phase's green time is exhausted
        if remaining <= 0:
            # Move to next phase
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

        # Still have time in current phase
        self._phase_remaining[tls_id] = remaining - self.decision_interval

        return PhaseCommand(
            action="hold",
            next_phase_id=current_phase,
            duration=self.decision_interval,
            reason_code="continuing",
        )

    def reset(self, tls_id: str = None):
        """Reset state"""
        if tls_id:
            self._phase_index.pop(tls_id, None)
            self._phase_remaining.pop(tls_id, None)
            self._current_plan.pop(tls_id, None)
        else:
            self._phase_index.clear()
            self._phase_remaining.clear()
            self._current_plan.clear()
