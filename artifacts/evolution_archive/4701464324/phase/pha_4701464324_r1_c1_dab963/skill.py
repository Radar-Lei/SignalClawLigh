from typing import Dict, Optional
from signalclaw.core.state import NetworkObservation, CyclePlan, PhaseCommand

_MIN_GREEN = 10.0
_MAX_GREEN = 60.0
_MAX_EXTEND = 5.0
_MAX_SHORTEN = 5.0
_DECISION_INTERVAL = 5.0

_phase_index: int = 0
_phase_remaining: float = 0.0
_current_plan_hash: int = 0


def _plan_hash(plan: "CyclePlan") -> int:
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def _calc_demand(phase_obs) -> float:
    """Calculate phase demand using the same scoring formula as CyclePlan."""
    if phase_obs is None:
        return 0.0
    q = getattr(phase_obs, 'queue', 0.0)
    arr = getattr(phase_obs, 'predicted_arrival', 0.0)
    wt = getattr(phase_obs, 'waiting_time', 0.0)
    return q + arr * 1.5 + wt * 0.5


def _get_downstream_risk(ego) -> float:
    """Aggregate downstream queue and spillback risk."""
    risk = 0.0
    if hasattr(ego, 'downstream_queue') and ego.downstream_queue:
        risk += sum(ego.downstream_queue.values())
    if hasattr(ego, 'downstream_spillback_risk'):
        risk += ego.downstream_spillback_risk * 20.0
    return risk


def decide(obs: "NetworkObservation", plan: "CyclePlan") -> "PhaseCommand":
    global _phase_index, _phase_remaining, _current_plan_hash

    ego = obs.ego
    phase_order = plan.phase_order

    if not phase_order:
        return PhaseCommand(
            action="hold", next_phase_id=ego.current_phase_id,
            duration=_DECISION_INTERVAL, reason_code="no_phases",
        )

    ph = _plan_hash(plan)
    if _current_plan_hash != ph:
        _phase_index = 0
        _current_plan_hash = ph
        first_phase = phase_order[0]
        _phase_remaining = plan.green_times.get(first_phase, 15.0)
        return PhaseCommand(
            action="switch", next_phase_id=first_phase,
            duration=_phase_remaining, reason_code="new_plan",
        )

    remaining = _phase_remaining
    current_phase = phase_order[_phase_index]
    allocated_green = plan.green_times.get(current_phase, 15.0)
    elapsed = allocated_green - remaining

    # Phase ended, switch to next
    if remaining <= 0:
        next_idx = (_phase_index + 1) % len(phase_order)
        next_phase = phase_order[next_idx]
        _phase_index = next_idx
        _phase_remaining = plan.green_times.get(next_phase, 15.0)
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=_phase_remaining, reason_code="phase_end",
        )

    # Get observations for current and next phase
    phase_obs = ego.phases.get(current_phase)
    current_queue = getattr(phase_obs, 'queue', 0.0) if phase_obs else 0.0
    current_demand = _calc_demand(phase_obs)

    next_idx = (_phase_index + 1) % len(phase_order)
    next_phase = phase_order[next_idx]
    next_obs = ego.phases.get(next_phase)
    next_demand = _calc_demand(next_obs)

    downstream_risk = _get_downstream_risk(ego)

    # --- EXTEND: high demand near phase end, downstream safe ---
    if remaining <= 10.0 and elapsed >= _MIN_GREEN:
        if current_demand > 3.0 and downstream_risk < 15.0:
            extend_amount = min(_MAX_EXTEND, max(0.5, current_demand * 0.4))
            total_green = elapsed + remaining + extend_amount
            if total_green <= _MAX_GREEN:
                _phase_remaining = remaining + extend_amount
                return PhaseCommand(
                    action="extend", next_phase_id=current_phase,
                    duration=_phase_remaining + _DECISION_INTERVAL,
                    reason_code="extend_d%.1f" % current_demand,
                )

    # --- SHORTEN: low current demand, high next demand ---
    if elapsed >= _MIN_GREEN and current_demand < 1.0:
        if next_demand > 5.0 and remaining > 2.0:
            shorten_amount = min(_MAX_SHORTEN, remaining - 1.0)
            if shorten_amount > 0.5:
                _phase_remaining = remaining - shorten_amount
                return PhaseCommand(
                    action="shorten", next_phase_id=current_phase,
                    duration=max(1.0, _phase_remaining),
                    reason_code="shorten_nd%.1f" % next_demand,
                )

    # --- EARLY SWITCH: no queue, next phase has demand ---
    if elapsed >= _MIN_GREEN:
        if current_queue < 0.5 and remaining > 5.0 and next_demand > 2.0:
            _phase_index = next_idx
            _phase_remaining = plan.green_times.get(next_phase, 15.0)
            return PhaseCommand(
                action="switch", next_phase_id=next_phase,
                duration=_phase_remaining,
                reason_code="early_switch_empty",
            )

    # Default: hold current phase
    _phase_remaining -= _DECISION_INTERVAL
    return PhaseCommand(
        action="hold", next_phase_id=current_phase,
        duration=_DECISION_INTERVAL, reason_code="continuing",
    )


def _reset():
    global _phase_index, _phase_remaining, _current_plan_hash
    _phase_index = 0
    _phase_remaining = 0.0
    _current_plan_hash = 0