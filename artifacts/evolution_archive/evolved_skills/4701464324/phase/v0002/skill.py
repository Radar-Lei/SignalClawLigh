from typing import Dict, Optional
from signalclaw.core.state import NetworkObservation, CyclePlan, PhaseCommand

_MIN_GREEN = 10.0
_MAX_GREEN = 60.0
_MAX_EXTEND = 5.0
_MAX_SHORTEN = 5.0
_DECISION_INTERVAL = 5.0


def _plan_hash(plan):
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def _calc_demand(phase_obs):
    """Calculate phase demand matching CyclePlan scoring formula."""
    if phase_obs is None:
        return 0.0
    q = getattr(phase_obs, 'queue', 0.0)
    arr = getattr(phase_obs, 'predicted_arrival', 0.0)
    wt = getattr(phase_obs, 'waiting_time', 0.0)
    return q + arr * 1.5 + wt * 0.5


def decide(obs, plan):
    # Use function attributes instead of global mutable state
    if not hasattr(decide, '_phase_index'):
        decide._phase_index = 0
        decide._phase_remaining = 0.0
        decide._current_plan_hash = 0

    ego = obs.ego
    phase_order = plan.phase_order

    if not phase_order:
        return PhaseCommand(
            action="hold",
            next_phase_id=getattr(ego, 'current_phase_id', 0),
            duration=_DECISION_INTERVAL,
            reason_code="no_phases",
        )

    # Detect plan change
    ph = _plan_hash(plan)
    if decide._current_plan_hash != ph:
        decide._phase_index = 0
        decide._current_plan_hash = ph
        first_phase = phase_order[0]
        decide._phase_remaining = plan.green_times.get(first_phase, 15.0)
        return PhaseCommand(
            action="switch",
            next_phase_id=first_phase,
            duration=decide._phase_remaining,
            reason_code="new_plan",
        )

    remaining = decide._phase_remaining
    idx = decide._phase_index
    current_phase = phase_order[idx]
    allocated_green = plan.green_times.get(current_phase, 15.0)
    elapsed = allocated_green - remaining

    # Phase ended naturally, advance to next
    if remaining <= 0:
        next_idx = (idx + 1) % len(phase_order)
        next_phase = phase_order[next_idx]
        decide._phase_index = next_idx
        decide._phase_remaining = plan.green_times.get(next_phase, 15.0)
        return PhaseCommand(
            action="switch",
            next_phase_id=next_phase,
            duration=decide._phase_remaining,
            reason_code="phase_end",
        )

    # Gather observations for current phase
    phase_obs = ego.phases.get(current_phase)
    current_queue = getattr(phase_obs, 'queue', 0.0) if phase_obs else 0.0
    current_arrival = getattr(phase_obs, 'predicted_arrival', 0.0) if phase_obs else 0.0
    current_wait = getattr(phase_obs, 'waiting_time', 0.0) if phase_obs else 0.0
    current_demand = _calc_demand(phase_obs)

    # Gather observations for next phase
    next_idx = (idx + 1) % len(phase_order)
    next_phase = phase_order[next_idx]
    next_obs = ego.phases.get(next_phase)
    next_demand = _calc_demand(next_obs)
    next_queue = getattr(next_obs, 'queue', 0.0) if next_obs else 0.0
    next_wait = getattr(next_obs, 'waiting_time', 0.0) if next_obs else 0.0

    # Downstream spillback risk
    spillback_risk = getattr(ego, 'downstream_spillback_risk', 0.0)

    # ---- PRIORITY 1: SHORTEN due to high downstream spillback risk ----
    if elapsed >= _MIN_GREEN and spillback_risk > 0.5 and remaining > 2.0:
        shorten_amount = min(_MAX_SHORTEN, remaining - 1.0)
        if shorten_amount > 0.5:
            decide._phase_remaining = remaining - shorten_amount
            return PhaseCommand(
                action="shorten",
                next_phase_id=current_phase,
                duration=max(1.0, decide._phase_remaining),
                reason_code="shorten_spillback%.1f" % spillback_risk,
            )

    # ---- PRIORITY 2: SHORTEN when current phase is empty and next has demand ----
    if elapsed >= _MIN_GREEN:
        is_empty = current_queue < 0.5 and current_arrival < 1.0
        next_needs_service = next_demand > 3.0 or next_queue > 2.0
        if is_empty and next_needs_service and remaining > 2.0:
            shorten_amount = min(_MAX_SHORTEN, remaining - 1.0)
            if shorten_amount > 0.5:
                decide._phase_remaining = remaining - shorten_amount
                return PhaseCommand(
                    action="shorten",
                    next_phase_id=current_phase,
                    duration=max(1.0, decide._phase_remaining),
                    reason_code="shorten_empty",
                )

    # ---- PRIORITY 3: EXTEND for high demand near phase end ----
    if remaining <= 10.0 and elapsed >= _MIN_GREEN:
        if current_demand > 2.5 and spillback_risk < 0.4:
            extend_amount = min(_MAX_EXTEND, max(0.5, current_demand * 0.35))
            # Dampen extend if next phase is even more demanding
            if next_demand > current_demand * 1.2:
                extend_amount *= 0.5
            total_green = elapsed + remaining + extend_amount
            if total_green <= _MAX_GREEN and extend_amount > 0.5:
                decide._phase_remaining = remaining + extend_amount
                return PhaseCommand(
                    action="extend",
                    next_phase_id=current_phase,
                    duration=decide._phase_remaining + _DECISION_INTERVAL,
                    reason_code="extend_d%.1f" % current_demand,
                )

    # ---- PRIORITY 4: EXTEND for long waiting vehicles ----
    if remaining <= 8.0 and elapsed >= _MIN_GREEN:
        if current_wait > 25.0 and current_queue > 1.5 and spillback_risk < 0.4:
            extend_amount = min(3.0, remaining * 0.4)
            total_green = elapsed + remaining + extend_amount
            if total_green <= _MAX_GREEN and extend_amount > 0.5:
                decide._phase_remaining = remaining + extend_amount
                return PhaseCommand(
                    action="extend",
                    next_phase_id=current_phase,
                    duration=decide._phase_remaining + _DECISION_INTERVAL,
                    reason_code="extend_wait%.1f" % current_wait,
                )

    # ---- PRIORITY 5: EARLY SWITCH when completely empty ----
    if elapsed >= _MIN_GREEN:
        if current_queue < 0.3 and current_arrival < 0.5 and remaining > 5.0 and next_demand > 2.0:
            decide._phase_index = next_idx
            decide._phase_remaining = plan.green_times.get(next_phase, 15.0)
            return PhaseCommand(
                action="switch",
                next_phase_id=next_phase,
                duration=decide._phase_remaining,
                reason_code="early_switch_empty",
            )

    # ---- DEFAULT: hold current phase ----
    decide._phase_remaining -= _DECISION_INTERVAL
    return PhaseCommand(
        action="hold",
        next_phase_id=current_phase,
        duration=_DECISION_INTERVAL,
        reason_code="continuing",
    )