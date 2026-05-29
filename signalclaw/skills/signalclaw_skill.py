import math
from typing import Dict, List, Optional, Tuple
from collections import deque
from signalclaw.core.state import (
    NetworkObservation, IntersectionObservation,
    CyclePlan, PhaseCommand, PhaseObservation
)


class SignalClawCyclePlanner:
    """
    Cycle-level planner that uses multi-factor scoring to allocate
    green time more intelligently than Max Pressure.

    Key advantages over Max Pressure:
    1. Considers predicted arrivals (not just current queue)
    2. Penalizes downstream spillback risk
    3. Prevents phase starvation with a hunger bonus
    4. Uses exponential smoothing for stable decisions
    5. Adapts cycle length to demand
    """

    def __init__(self, min_green: float = 10.0, max_green: float = 60.0,
                 base_cycle: float = 80.0, history_window: int = 5):
        self.min_green = min_green
        self.max_green = max_green
        self.base_cycle = base_cycle
        self.history_window = history_window
        # Per-intersection state
        self._last_green_time: Dict[str, Dict[int, float]] = {}  # tls_id -> {phase_id -> sim_time}
        self._pressure_history: Dict[str, Dict[int, deque]] = {}  # tls_id -> {phase_id -> deque of pressures}
        self._last_cycle_plan: Dict[str, CyclePlan] = {}

    def _get_pressure_history(self, tls_id: str, phase_id: int) -> deque:
        if tls_id not in self._pressure_history:
            self._pressure_history[tls_id] = {}
        if phase_id not in self._pressure_history[tls_id]:
            self._pressure_history[tls_id][phase_id] = deque(maxlen=self.history_window)
        return self._pressure_history[tls_id][phase_id]

    def _compute_advanced_pressure(self, obs: IntersectionObservation, phase_id: int,
                                   sim_time: float) -> float:
        """
        Compute an advanced pressure score that considers multiple factors.

        score = w1 * local_pressure
              + w2 * arrival_prediction
              - w3 * spillback_risk
              + w4 * hunger_bonus
        """
        phase_obs = obs.phases.get(phase_id)
        if phase_obs is None:
            return 0.0

        # Factor 1: Local pressure (upstream queue - normalized downstream queue)
        local_pressure = phase_obs.queue

        # Downstream pressure - only penalize if downstream is congested
        downstream_total = sum(obs.downstream_queue.values()) if obs.downstream_queue else 0.0
        n_downstream = max(len(obs.downstream_queue), 1)
        avg_downstream = downstream_total / n_downstream

        # Spillback risk: if downstream has many vehicles, reduce pressure
        spillback_risk = max(0, avg_downstream - 5.0) * 2.0  # Only penalize above threshold

        # Factor 2: Predicted arrivals
        # Use waiting time as a proxy for buildup
        arrival_prediction = phase_obs.waiting_time * 0.3

        # Factor 3: Phase hunger bonus
        # Phases that haven't been served recently get a bonus
        last_served = self._last_green_time.get(obs.crossing_id, {}).get(phase_id, 0)
        hunger_time = sim_time - last_served
        hunger_bonus = min(hunger_time * 0.5, 15.0)  # Cap at 15 bonus points

        # Weighted combination
        score = (
            1.0 * local_pressure          # Current queue pressure
            + 0.4 * arrival_prediction     # Future demand
            - 0.8 * spillback_risk         # Downstream blockage risk
            + 0.6 * hunger_bonus           # Phase starvation prevention
        )

        return score

    def plan(self, obs: NetworkObservation) -> CyclePlan:
        ego = obs.ego
        tls_id = ego.crossing_id
        sim_time = obs.timestamp

        green_phases = sorted(ego.phases.keys())
        if not green_phases:
            return CyclePlan(cycle_length=self.base_cycle, green_times={}, phase_order=[])

        # Compute advanced pressure for each phase
        scores = {}
        for gp in green_phases:
            score = self._compute_advanced_pressure(ego, gp, sim_time)
            scores[gp] = score
            # Record in history
            hist = self._get_pressure_history(tls_id, gp)
            hist.append(score)

        # Adaptive cycle length based on total demand
        total_queue = sum(p.queue for p in ego.phases.values())
        if total_queue < 5:
            cycle_length = self.base_cycle * 0.7  # Low demand: shorter cycle
        elif total_queue > 50:
            cycle_length = self.base_cycle * 1.3  # High demand: longer cycle
        else:
            # Linear interpolation
            cycle_length = self.base_cycle * (0.7 + 0.6 * (total_queue - 5) / 45.0)
        cycle_length = max(40.0, min(180.0, cycle_length))

        # Allocate green time using softmax-like allocation
        # Shift scores to be positive
        min_score = min(scores.values())
        shifted = {gp: max(s - min_score + 1.0, 0.1) for gp, s in scores.items()}

        # Apply exponential smoothing with history
        for gp in green_phases:
            hist = self._get_pressure_history(tls_id, gp)
            if len(hist) > 1:
                # Smooth: 70% current score + 30% historical average
                avg_hist = sum(hist) / len(hist)
                shifted[gp] = 0.7 * shifted[gp] + 0.3 * max(avg_hist - min_score + 1.0, 0.1)

        total_score = sum(shifted.values())

        green_times = {}
        for gp in green_phases:
            if total_score > 0:
                gt = cycle_length * (shifted[gp] / total_score)
            else:
                gt = cycle_length / len(green_phases)
            green_times[gp] = max(self.min_green, min(self.max_green, gt))

        # Record this plan
        plan = CyclePlan(
            cycle_length=sum(green_times.values()),
            green_times=green_times,
            phase_order=green_phases,
        )
        self._last_cycle_plan[tls_id] = plan

        return plan

    def record_phase_served(self, tls_id: str, phase_id: int, sim_time: float):
        """Record that a phase was served (for hunger tracking)"""
        if tls_id not in self._last_green_time:
            self._last_green_time[tls_id] = {}
        self._last_green_time[tls_id][phase_id] = sim_time

    def reset(self):
        self._last_green_time.clear()
        self._pressure_history.clear()
        self._last_cycle_plan.clear()


class SignalClawMicroAdjuster:
    """
    Phase-level micro-adjuster that makes real-time decisions
    to extend or shorten green phases based on current conditions.

    This operates within the framework set by the CyclePlanner.
    """

    def __init__(self, extend_threshold: float = 3.0,
                 shorten_threshold: float = -2.0,
                 max_extend: float = 5.0, max_shorten: float = 5.0,
                 decision_interval: float = 5.0):
        self.extend_threshold = extend_threshold
        self.shorten_threshold = shorten_threshold
        self.max_extend = max_extend
        self.max_shorten = max_shorten
        self.decision_interval = decision_interval
        self._phase_index: Dict[str, int] = {}
        self._phase_remaining: Dict[str, float] = {}
        self._current_plan: Dict[str, CyclePlan] = {}

    def decide(self, obs: NetworkObservation, plan: CyclePlan) -> PhaseCommand:
        ego = obs.ego
        tls_id = ego.crossing_id

        phase_order = plan.phase_order
        if not phase_order:
            return PhaseCommand(
                action="hold", next_phase_id=ego.current_phase_id,
                duration=self.decision_interval, reason_code="no_phases"
            )

        # Get or initialize phase tracking
        if tls_id not in self._phase_index or self._current_plan.get(tls_id) != plan:
            self._phase_index[tls_id] = 0
            self._current_plan[tls_id] = plan
            first_phase = phase_order[0]
            self._phase_remaining[tls_id] = plan.green_times.get(first_phase, 15.0)
            return PhaseCommand(
                action="switch", next_phase_id=first_phase,
                duration=plan.green_times.get(first_phase, 15.0),
                reason_code="new_plan"
            )

        current_idx = self._phase_index[tls_id]
        remaining = self._phase_remaining[tls_id]

        if remaining <= 0:
            # Time to switch to next phase
            next_idx = (current_idx + 1) % len(phase_order)
            next_phase = phase_order[next_idx]
            self._phase_index[tls_id] = next_idx
            self._phase_remaining[tls_id] = plan.green_times.get(next_phase, 15.0)

            return PhaseCommand(
                action="switch",
                next_phase_id=next_phase,
                duration=self._phase_remaining[tls_id],
                reason_code="phase_end",
            )

        # Micro-adjustment logic
        current_phase = phase_order[current_idx]
        phase_obs = ego.phases.get(current_phase)

        if phase_obs is not None and remaining <= 10.0:
            # In the last 10 seconds, consider micro-adjustments

            # Compute real-time pressure change
            current_queue = phase_obs.queue
            downstream_risk = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0

            # If this phase still has significant demand and downstream is clear
            if current_queue > self.extend_threshold and downstream_risk < 10:
                # Extend by a small amount
                extend = min(self.max_extend, self.decision_interval)
                self._phase_remaining[tls_id] = remaining + extend
                self._phase_remaining[tls_id] -= self.decision_interval

                return PhaseCommand(
                    action="extend",
                    next_phase_id=current_phase,
                    duration=self._phase_remaining[tls_id] + self.decision_interval,
                    reason_code=f"extend_high_demand_q{current_queue:.0f}",
                )

            # If queue is very low, consider shortening
            if current_queue < 1.0 and remaining > 5.0:
                # Skip remaining time and move to next phase
                self._phase_remaining[tls_id] = 0

                next_idx = (current_idx + 1) % len(phase_order)
                next_phase = phase_order[next_idx]
                self._phase_index[tls_id] = next_idx
                self._phase_remaining[tls_id] = plan.green_times.get(next_phase, 15.0)

                return PhaseCommand(
                    action="switch",
                    next_phase_id=next_phase,
                    duration=self._phase_remaining[tls_id],
                    reason_code=f"early_switch_empty_q{current_queue:.0f}",
                )

        # Default: hold current phase
        self._phase_remaining[tls_id] -= self.decision_interval

        return PhaseCommand(
            action="hold",
            next_phase_id=current_phase,
            duration=self.decision_interval,
            reason_code="continuing",
        )

    def reset(self):
        self._phase_index.clear()
        self._phase_remaining.clear()
        self._current_plan.clear()


class SignalClawSkill:
    """
    Combined SignalClaw skill: CyclePlanner + MicroAdjuster.
    This is the main class used by the experiment runner.
    """

    def __init__(self, decision_interval: float = 5.0, **kwargs):
        # Filter kwargs to only those accepted by CyclePlanner
        planner_kwargs = {k: v for k, v in kwargs.items()
                          if k in ('min_green', 'max_green', 'base_cycle', 'history_window')}
        self.planner = SignalClawCyclePlanner(**planner_kwargs)
        self.adjuster = SignalClawMicroAdjuster(decision_interval=decision_interval)
        self._current_plan: Dict[str, CyclePlan] = {}
        self._plan_interval: float = 0.0  # Re-plan every cycle

    def plan_cycle(self, obs: NetworkObservation) -> CyclePlan:
        plan = self.planner.plan(obs)
        self._current_plan[obs.ego.crossing_id] = plan
        return plan

    def decide(self, obs: NetworkObservation) -> PhaseCommand:
        tls_id = obs.ego.crossing_id
        plan = self._current_plan.get(tls_id)
        if plan is None:
            plan = self.plan_cycle(obs)

        cmd = self.adjuster.decide(obs, plan)

        # Record phase served for hunger tracking
        if cmd.action == "switch":
            self.planner.record_phase_served(tls_id, obs.ego.current_phase_id, obs.timestamp)
            # Check if we completed a full cycle - if so, re-plan
            phase_order = plan.phase_order
            current_idx = self.adjuster._phase_index.get(tls_id, 0)
            if current_idx == 0:
                new_plan = self.plan_cycle(obs)
                # Update the adjuster's plan
                self.adjuster._current_plan[tls_id] = new_plan

        return cmd

    def reset(self):
        self.planner.reset()
        self.adjuster.reset()
        self._current_plan.clear()
