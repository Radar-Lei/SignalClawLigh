import math

_decision_interval = 3.0
_min_green = 10.0
_max_green = 60.0
_max_extend = 5.0
_max_shorten = 5.0

_phase_index = 0
_phase_remaining = 0.0
_current_plan_key = None


def _get_plan_key(plan):
    """获取计划的关键特征，用于检测计划变化"""
    return (plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order))


def _calc_phase_score(phase_obs, ego):
    """与CyclePlan完全对齐的评分逻辑，包含下游惩罚"""
    if phase_obs is None:
        return 0.1

    local_pressure = phase_obs.queue + phase_obs.predicted_arrival
    hunger_bonus = phase_obs.waiting_time * 0.3
    upstream_bonus = ego.upstream_release_pressure * 0.2

    downstream_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
    n_downstream = max(len(ego.downstream_queue), 1)
    avg_downstream = downstream_total / n_downstream
    spillback_penalty = max(0.0, avg_downstream - 5.0) * 2.0

    score = local_pressure + hunger_bonus + upstream_bonus - spillback_penalty
    return max(score, 0.1)


def decide(obs, plan):
    global _phase_index, _phase_remaining, _current_plan_key

    ego = obs.ego
    phase_order = plan.phase_order

    if not phase_order:
        return PhaseCommand(
            action="hold", next_phase_id=ego.current_phase_id,
            duration=_decision_interval, reason_code="no_phases",
        )

    key = _get_plan_key(plan)
    if _current_plan_key != key:
        _phase_index = 0
        _current_plan_key = key
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

    # 下游状态
    downstream_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
    spillback_risk = ego.downstream_spillback_risk
    n_downstream = max(len(ego.downstream_queue), 1)
    avg_downstream = downstream_total / n_downstream
    downstream_safe = avg_downstream < 10.0 and spillback_risk < 0.5

    current_score = _calc_phase_score(phase_obs, ego)

    n_phases = len(phase_order)
    next_idx = (_phase_index + 1) % n_phases
    next_phase = phase_order[next_idx]
    next_phase_obs = ego.phases.get(next_phase)
    next_score = _calc_phase_score(next_phase_obs, ego)

    # 其他相位统计
    other_total_score = 0.0
    max_other_waiting = 0.0
    for i in range(n_phases):
        if i != _phase_index:
            po = ego.phases.get(phase_order[i])
            other_total_score += _calc_phase_score(po, ego)
            if po and po.waiting_time > max_other_waiting:
                max_other_waiting = po.waiting_time

    planned_green = plan.green_times.get(current_phase, 15.0)
    elapsed_est = max(planned_green - remaining, 0.0)
    progress = elapsed_est / planned_green if planned_green > 0 else 1.0

    # 1. 时间用完 -> 切换
    if remaining <= 0:
        _phase_index = next_idx
        next_green = plan.green_times.get(next_phase, 15.0)
        _phase_remaining = next_green
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=next_green, reason_code="phase_end",
        )

    # 2. 最小绿灯保护
    if elapsed_est < _min_green:
        _phase_remaining -= _decision_interval
        return PhaseCommand(
            action="hold", next_phase_id=current_phase,
            duration=_decision_interval, reason_code="min_green",
        )

    # 3. 最大绿灯限制
    if elapsed_est >= _max_green:
        _phase_index = next_idx
        next_green = plan.green_times.get(next_phase, 15.0)
        _phase_remaining = next_green
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=next_green, reason_code="max_green_reached",
        )

    # 4. 后半段微调
    if progress > 0.5:
        # 4a. 溢出风险 + 低需求 -> 缩短（防止拥堵扩散）
        if spillback_risk > 0.6 and current_queue < 2.0:
            shorten_amount = min(_max_shorten, remaining * 0.4)
            if shorten_amount > 1.0 and remaining - shorten_amount >= 2.0:
                new_remaining = remaining - shorten_amount
                _phase_remaining = new_remaining
                return PhaseCommand(
                    action="shorten", next_phase_id=current_phase,
                    duration=new_remaining, reason_code="shorten_spillback",
                )

        # 4b. 完全空闲 + 其他有需求 -> 提前切换
        if current_queue < 0.3 and current_score < 1.0 and other_total_score > 5.0:
            _phase_index = next_idx
            next_green = plan.green_times.get(next_phase, 15.0)
            _phase_remaining = next_green
            return PhaseCommand(
                action="switch", next_phase_id=next_phase,
                duration=next_green, reason_code="early_switch_empty",
            )

        # 4c. 其他相位严重饥饿 -> 适度缩短（防止不公平）
        if max_other_waiting > 30.0 and current_queue < 3.0:
            shorten_amount = min(_max_shorten * 0.5, remaining * 0.25)
            if shorten_amount > 0.5 and remaining - shorten_amount >= 2.0:
                new_remaining = remaining - shorten_amount
                _phase_remaining = new_remaining
                return PhaseCommand(
                    action="shorten", next_phase_id=current_phase,
                    duration=new_remaining, reason_code="shorten_hunger",
                )

        # 4d. 下一相位需求显著更高 -> 适度缩短
        if next_score > current_score * 2.0 and next_score > 3.0:
            shorten_amount = min(_max_shorten * 0.6, remaining * 0.3)
            if shorten_amount > 1.0 and remaining - shorten_amount >= 2.0:
                new_remaining = remaining - shorten_amount
                _phase_remaining = new_remaining
                return PhaseCommand(
                    action="shorten", next_phase_id=current_phase,
                    duration=new_remaining, reason_code="shorten_next_demand",
                )

        # 4e. 当前需求高 + 下游安全 -> 延长
        if current_score > 3.0 and downstream_safe:
            extend_amount = min(_max_extend, current_score * 0.35)
            # 确保不超过max_green
            max_possible = _max_green - elapsed_est - remaining
            extend_amount = min(extend_amount, max(0, max_possible))

            if extend_amount > 0.5:
                new_remaining = remaining + extend_amount
                _phase_remaining = new_remaining - _decision_interval
                return PhaseCommand(
                    action="extend", next_phase_id=current_phase,
                    duration=new_remaining,
                    reason_code="extend_demand",
                )

    # 5. 正常保持
    _phase_remaining -= _decision_interval
    return PhaseCommand(
        action="hold", next_phase_id=current_phase,
        duration=_decision_interval, reason_code="continuing",
    )