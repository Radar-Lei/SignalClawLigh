import math

_decision_interval = 3.0
_min_green = 10.0
_max_green = 60.0
_max_extend = 5.0
_max_shorten = 5.0

_extend_score_threshold = 2.5
_early_switch_queue = 0.5
_other_score_for_early = 5.0
_shorten_queue_for_spillback = 1.0
_downstream_queue_safe = 12.0
_spillback_risk_safe = 0.5

_phase_index = 0
_phase_remaining = 0.0
_current_plan_hash = 0


def _plan_hash(plan):
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def _calc_phase_score(phase_obs, ego):
    """计算相位评分，与CyclePlan的评分逻辑对齐"""
    if phase_obs is None:
        return 0.1
    local_pressure = phase_obs.queue + phase_obs.predicted_arrival
    hunger_bonus = phase_obs.waiting_time * 0.3
    upstream_bonus = ego.upstream_release_pressure * 0.2
    return max(local_pressure + hunger_bonus + upstream_bonus, 0.1)


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
        first_green = plan.green_times.get(first_phase, 15.0)
        _phase_remaining = first_green
        return PhaseCommand(
            action="switch", next_phase_id=first_phase,
            duration=first_green, reason_code="new_plan",
        )

    remaining = _phase_remaining
    current_phase = phase_order[_phase_index]
    phase_obs = ego.phases.get(current_phase)
    current_queue = phase_obs.queue if phase_obs else 0.0

    downstream_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
    spillback_risk = ego.downstream_spillback_risk
    downstream_safe = downstream_total < _downstream_queue_safe and spillback_risk < _spillback_risk_safe

    current_score = _calc_phase_score(phase_obs, ego)

    n_phases = len(phase_order)
    next_idx = (_phase_index + 1) % n_phases
    next_phase = phase_order[next_idx]
    next_phase_obs = ego.phases.get(next_phase)
    next_score = _calc_phase_score(next_phase_obs, ego)

    other_total_score = 0.0
    for i in range(n_phases):
        if i != _phase_index:
            po = ego.phases.get(phase_order[i])
            other_total_score += _calc_phase_score(po, ego)

    planned_green = plan.green_times.get(current_phase, 15.0)
    elapsed_est = max(planned_green - remaining, 0.0)

    # 1. Time expired -> switch
    if remaining <= 0:
        _phase_index = next_idx
        next_green = plan.green_times.get(next_phase, 15.0)
        _phase_remaining = next_green
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=next_green, reason_code="phase_end",
        )

    # 2. Minimum green protection
    if elapsed_est < _min_green:
        _phase_remaining -= _decision_interval
        return PhaseCommand(
            action="hold", next_phase_id=current_phase,
            duration=_decision_interval, reason_code="min_green",
        )

    # 3. Maximum green enforcement
    if elapsed_est >= _max_green:
        _phase_index = next_idx
        next_green = plan.green_times.get(next_phase, 15.0)
        _phase_remaining = next_green
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=next_green, reason_code="max_green_reached",
        )

    # 4. Second half fine-tuning
    if remaining <= planned_green * 0.5:
        # 4a. High demand + downstream safe -> extend
        if current_score > _extend_score_threshold and downstream_safe:
            extend_amount = min(_max_extend, current_score * 0.4)
            if extend_amount > 0:
                new_remaining = remaining + extend_amount
                _phase_remaining = new_remaining - _decision_interval
                return PhaseCommand(
                    action="extend", next_phase_id=current_phase,
                    duration=new_remaining,
                    reason_code=f"extend_s{current_score:.1f}",
                )

        # 4b. Empty phase + other demand -> early switch
        if current_queue < _early_switch_queue and other_total_score > _other_score_for_early:
            _phase_index = next_idx
            next_green = plan.green_times.get(next_phase, 15.0)
            _phase_remaining = next_green
            return PhaseCommand(
                action="switch", next_phase_id=next_phase,
                duration=next_green, reason_code="early_switch_empty",
            )

        # 4c. Spillback risk + low demand -> shorten
        if not downstream_safe and current_queue < _shorten_queue_for_spillback:
            shorten_amount = min(_max_shorten, remaining * 0.4)
            if shorten_amount > 0:
                new_remaining = remaining - shorten_amount
                _phase_remaining = new_remaining
                return PhaseCommand(
                    action="shorten", next_phase_id=current_phase,
                    duration=new_remaining, reason_code="shorten_spillback",
                )

        # 4d. Next phase much higher demand -> moderate shorten
        if next_score > current_score * 2.0 and next_score > 3.0:
            shorten_amount = min(_max_shorten * 0.5, remaining * 0.3)
            if shorten_amount > 0:
                new_remaining = remaining - shorten_amount
                _phase_remaining = new_remaining
                return PhaseCommand(
                    action="shorten", next_phase_id=current_phase,
                    duration=new_remaining, reason_code="shorten_next_demand",
                )

    # 5. Normal hold
    _phase_remaining -= _decision_interval
    return PhaseCommand(
        action="hold", next_phase_id=current_phase,
        duration=_decision_interval, reason_code="continuing",
    )