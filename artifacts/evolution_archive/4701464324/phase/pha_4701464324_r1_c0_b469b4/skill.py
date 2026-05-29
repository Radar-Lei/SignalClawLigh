"""Improved PhaseMicroSkill for intersection 4701464324."""

_decision_interval = 5.0
_max_extend = 5.0
_max_shorten = 5.0
_min_green = 10.0
_max_green = 60.0

_phase_index = 0
_phase_remaining = 0.0
_current_plan_hash = 0


def _plan_hash(plan):
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def _score_phase(phase_obs):
    """Score a phase using the same formula as CyclePlan: demand + urgency."""
    if phase_obs is None:
        return 0.1
    q = phase_obs.queue
    arr = phase_obs.predicted_arrival
    wt = phase_obs.waiting_time
    demand = q + arr * 1.5
    urgency = wt * 0.5
    return max(demand + urgency, 0.1)


def decide(obs, plan):
    global _phase_index, _phase_remaining, _current_plan_hash

    ego = obs.ego
    phase_order = plan.phase_order
    if not phase_order:
        return PhaseCommand(
            action="hold", next_phase_id=ego.current_phase_id,
            duration=_decision_interval, reason_code="no_phases",
        )

    ph = _plan_hash(plan)
    if _current_plan_hash != ph:
        _phase_index = 0
        _current_plan_hash = ph
        first_phase = phase_order[0]
        _phase_remaining = plan.green_times.get(first_phase, 15.0)
        return PhaseCommand(
            action="switch", next_phase_id=first_phase,
            duration=plan.green_times.get(first_phase, 15.0),
            reason_code="new_plan",
        )

    remaining = _phase_remaining

    # Phase exhausted -> advance to next
    if remaining <= 0:
        next_idx = (_phase_index + 1) % len(phase_order)
        next_phase = phase_order[next_idx]
        _phase_index = next_idx
        _phase_remaining = plan.green_times.get(next_phase, 15.0)
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=_phase_remaining, reason_code="phase_end",
        )

    current_phase = phase_order[_phase_index]
    phase_obs = ego.phases.get(current_phase)

    # Score current phase
    current_score = _score_phase(phase_obs)

    # Next phase info
    next_idx = (_phase_index + 1) % len(phase_order)
    next_phase_id = phase_order[next_idx]
    next_obs = ego.phases.get(next_phase_id)
    next_score = _score_phase(next_obs)

    # Max urgency among all non-current phases
    other_max_score = 0.1
    for i, pid in enumerate(phase_order):
        if i != _phase_index:
            po = ego.phases.get(pid)
            s = _score_phase(po)
            if s > other_max_score:
                other_max_score = s

    # Downstream spillback risk
    downstream_risk = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0

    # Extract current phase state
    current_queue = 0.0
    current_arrival = 0.0
    if phase_obs is not None:
        current_queue = phase_obs.queue
        current_arrival = phase_obs.predicted_arrival

    # ---- Near end-of-phase micro-adjustments (remaining <= 15s) ----
    if remaining <= 15.0:

        # === EXTEND ===
        should_extend = False
        extend_reason = "none"

        # Primary: high queue + downstream clear + more urgent than next phase
        if current_queue > 3.0 and downstream_risk < 10.0:
            if current_score > next_score * 1.2:
                should_extend = True
                extend_reason = "demand"

        # Secondary: arrival surge coming + downstream clear
        if (not should_extend
                and current_arrival > 5.0
                and current_queue > 2.0
                and downstream_risk < 8.0):
            should_extend = True
            extend_reason = "surge"

        # Safety: block extend when downstream is highly congested
        if downstream_risk >= 12.0:
            should_extend = False

        if should_extend:
            extend_amount = min(_max_extend, _decision_interval)
            if current_queue > 10.0:
                extend_amount = _max_extend

            _phase_remaining = remaining + extend_amount - _decision_interval
            new_duration = _phase_remaining + _decision_interval
            if new_duration > _max_green:
                new_duration = _max_green
                _phase_remaining = new_duration - _decision_interval

            return PhaseCommand(
                action="extend", next_phase_id=current_phase,
                duration=new_duration,
                reason_code=f"extend_{extend_reason}_q{current_queue:.0f}",
            )

        # === EARLY SWITCH ===
        if current_queue < 1.0 and remaining > 5.0:
            _phase_remaining = 0
            _phase_index = next_idx
            _phase_remaining = plan.green_times.get(next_phase_id, 15.0)
            return PhaseCommand(
                action="switch", next_phase_id=next_phase_id,
                duration=_phase_remaining,
                reason_code=f"early_switch_q{current_queue:.0f}",
            )

        # === SHORTEN ===
        if (current_queue < 2.0
                and current_arrival < 2.0
                and remaining > 8.0
                and next_score > current_score * 2.5):
            shorten_amount = min(_max_shorten, 3.0)
            new_remaining = remaining - shorten_amount
            if new_remaining < 5.0:
                new_remaining = 5.0
            _phase_remaining = new_remaining
            return PhaseCommand(
                action="shorten", next_phase_id=current_phase,
                duration=new_remaining,
                reason_code=f"shorten_q{current_queue:.0f}",
            )

    # ---- Mid-phase shorten when phase is clearly over-served ----
    if remaining > 15.0:
        if (current_queue < 0.5
                and current_arrival < 1.0
                and other_max_score > current_score * 4.0):
            shorten_amount = min(_max_shorten, 5.0)
            new_remaining = remaining - shorten_amount
            if new_remaining < _min_green:
                new_remaining = _min_green
            _phase_remaining = new_remaining
            return PhaseCommand(
                action="shorten", next_phase_id=current_phase,
                duration=new_remaining,
                reason_code=f"mid_shorten_q{current_queue:.0f}",
            )

    # Default: hold current phase
    _phase_remaining -= _decision_interval
    return PhaseCommand(
        action="hold", next_phase_id=current_phase,
        duration=_decision_interval, reason_code="continuing",
    )


def _reset():
    global _phase_index, _phase_remaining, _current_plan_hash
    _phase_index = 0
    _phase_remaining = 0.0
    _current_plan_hash = 0
