"""Improved PhaseMicroSkill for intersection 314655170."""
from typing import Dict, Optional
from signalclaw.core.state import NetworkObservation, CyclePlan, PhaseCommand

_extend_threshold = 3.0
_max_extend = 5.0
_max_shorten = 5.0
_decision_interval = 5.0
_min_green = 10.0
_max_green = 60.0

_phase_index: int = 0
_phase_remaining: float = 0.0
_current_plan_hash: int = 0
_total_adjusted: float = 0.0


def _plan_hash(plan: "CyclePlan") -> int:
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def _calc_demand(phase_obs) -> float:
    """Calculate phase demand consistent with CyclePlan algorithm."""
    if phase_obs is None:
        return 1.0
    queue = max(phase_obs.queue, 0.0)
    arrival = max(phase_obs.predicted_arrival, 0.0)
    waiting = max(phase_obs.waiting_time, 0.0)
    sat_flow = phase_obs.saturation_flow
    demand = queue * 2.0 + arrival
    if sat_flow > 0:
        time_needed = (queue + arrival) / sat_flow * 3600.0
        demand = max(demand, time_needed)
    demand += waiting * 0.2
    return max(demand, 0.1)


def _get_downstream_total(ego) -> float:
    """Get total downstream queue."""
    if not ego.downstream_queue:
        return 0.0
    return sum(ego.downstream_queue.values())


def decide(obs: "NetworkObservation", plan: "CyclePlan") -> "PhaseCommand":
    global _phase_index, _phase_remaining, _current_plan_hash, _total_adjusted

    ego = obs.ego
    phase_order = plan.phase_order

    if not phase_order:
        return PhaseCommand(
            action="hold", next_phase_id=ego.current_phase_id,
            duration=_decision_interval, reason_code="no_phases",
        )

    # Detect new plan
    ph = _plan_hash(plan)
    if _current_plan_hash != ph:
        _phase_index = 0
        _current_plan_hash = ph
        _total_adjusted = 0.0
        first_phase = phase_order[0]
        _phase_remaining = plan.green_times.get(first_phase, 15.0)
        return PhaseCommand(
            action="switch", next_phase_id=first_phase,
            duration=_phase_remaining,
            reason_code="new_plan",
        )

    remaining = _phase_remaining

    # Phase ended, switch to next
    if remaining <= 0:
        next_idx = (_phase_index + 1) % len(phase_order)
        next_phase = phase_order[next_idx]
        _phase_index = next_idx
        _total_adjusted = 0.0
        _phase_remaining = plan.green_times.get(next_phase, 15.0)
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=_phase_remaining,
            reason_code="phase_end",
        )

    current_phase = phase_order[_phase_index]
    phase_obs = ego.phases.get(current_phase)
    planned_green = plan.green_times.get(current_phase, 15.0)
    actual_green = planned_green + _total_adjusted
    elapsed = actual_green - remaining

    if phase_obs is not None:
        current_queue = max(phase_obs.queue, 0.0)
        current_arrival = max(phase_obs.predicted_arrival, 0.0)
        current_demand = _calc_demand(phase_obs)

        # Downstream queue assessment
        downstream_total = _get_downstream_total(ego)

        # Calculate time needed to clear queue
        time_to_clear = 0.0
        sat_flow = phase_obs.saturation_flow
        if sat_flow > 0:
            time_to_clear = (current_queue + current_arrival) / sat_flow * 3600.0

        # Next phase demand
        next_idx = (_phase_index + 1) % len(phase_order)
        next_phase = phase_order[next_idx]
        next_obs = ego.phases.get(next_phase)
        next_demand = _calc_demand(next_obs)

        # ===============================
        # EXTEND logic
        # ===============================
        # Conditions:
        # 1. Low remaining time (<= 12s)
        # 2. Can still extend (total adjustment < max_extend)
        # 3. Won't exceed max_green after extension
        # 4. Still has demand (clear time > remaining or high demand score)
        # 5. No severe downstream congestion
        can_extend_more = _total_adjusted < _max_extend
        within_max_green = actual_green < _max_green
        still_has_demand = time_to_clear > remaining + 2.0 or (current_demand > remaining * 1.5 and current_queue > 2.0)
        downstream_ok = downstream_total < 15.0

        if remaining <= 12.0 and can_extend_more and within_max_green and still_has_demand and downstream_ok:
            extend_amount = min(
                _max_extend - _total_adjusted,
                _max_green - actual_green,
                max(time_to_clear - remaining + 2.0, 0.0),
                _decision_interval
            )

            if extend_amount > 0.5:
                _total_adjusted += extend_amount
                _phase_remaining = remaining + extend_amount
                return PhaseCommand(
                    action="extend", next_phase_id=current_phase,
                    duration=_phase_remaining,
                    reason_code=f"extend_q{current_queue:.0f}_d{current_demand:.1f}",
                )

        # ===============================
        # SHORTEN logic
        # ===============================
        # Conditions:
        # 1. Min green satisfied (elapsed >= min_green)
        # 2. Enough room to shorten (remaining > 7s)
        # 3. One of:
        #    a. Downstream severe congestion + low current queue
        #    b. Current phase empty + high next phase demand
        min_green_met = elapsed >= _min_green
        has_room_to_shorten = remaining > 7.0

        if min_green_met and has_room_to_shorten:
            should_shorten = False
            shorten_reason = ""

            # Condition a: downstream spillback risk
            if downstream_total > 20.0 and current_queue < 2.0:
                should_shorten = True
                shorten_reason = f"shorten_spillback{downstream_total:.0f}"

            # Condition b: current empty and next phase has high demand
            if current_queue < 0.5 and next_demand > 8.0 and next_demand > current_demand * 3.0:
                should_shorten = True
                shorten_reason = f"shorten_empty_next{next_demand:.0f}"

            if should_shorten:
                shorten_amount = min(
                    remaining - 5.0,
                    actual_green - _min_green,
                    _decision_interval
                )

                if shorten_amount > 1.0:
                    _total_adjusted -= shorten_amount
                    _phase_remaining = remaining - shorten_amount
                    return PhaseCommand(
                        action="shorten", next_phase_id=current_phase,
                        duration=_phase_remaining,
                        reason_code=shorten_reason,
                    )

    # Normal continue
    _phase_remaining -= _decision_interval
    return PhaseCommand(
        action="hold", next_phase_id=current_phase,
        duration=_decision_interval, reason_code="continuing",
    )


def _reset():
    global _phase_index, _phase_remaining, _current_plan_hash, _total_adjusted
    _phase_index = 0
    _phase_remaining = 0.0
    _current_plan_hash = 0
    _total_adjusted = 0.0