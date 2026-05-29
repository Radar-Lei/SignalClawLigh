import math

# ===== 配置参数 =====
_decision_interval = 5.0
_min_green = 10.0
_max_green = 60.0
_max_extend = 5.0
_max_shorten = 5.0

# 阈值配置
_extend_demand_threshold = 3.0
_low_demand_threshold = 1.5
_next_urgent_threshold = 5.0
_downstream_risk_limit = 15.0
_downstream_spillback_limit = 0.5
_high_downstream_queue = 20.0

# ===== 状态 =====
_phase_index: int = 0
_phase_remaining: float = 0.0
_phase_elapsed: float = 0.0
_current_plan_hash: int = 0
_planned_duration: float = 15.0


def _plan_hash(plan):
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def _phase_demand(phase_obs):
    """计算相位需求分数，与CyclePlan的评分逻辑对齐"""
    if phase_obs is None:
        return 0.0
    return phase_obs.queue * 1.0 + phase_obs.predicted_arrival * 0.8 + phase_obs.waiting_time * 0.3


def decide(obs, plan):
    global _phase_index, _phase_remaining, _phase_elapsed, _current_plan_hash, _planned_duration

    ego = obs.ego
    phase_order = plan.phase_order

    if not phase_order:
        return PhaseCommand(
            action="hold", next_phase_id=ego.current_phase_id,
            duration=_decision_interval, reason_code="no_phases",
        )

    # 检测新计划
    ph = _plan_hash(plan)
    if _current_plan_hash != ph:
        _phase_index = 0
        _current_plan_hash = ph
        _phase_elapsed = 0.0
        first_phase = phase_order[0]
        _planned_duration = plan.green_times.get(first_phase, 15.0)
        _planned_duration = max(_min_green, min(_max_green, _planned_duration))
        _phase_remaining = _planned_duration
        return PhaseCommand(
            action="switch", next_phase_id=first_phase,
            duration=_planned_duration, reason_code="new_plan",
        )

    remaining = _phase_remaining
    current_phase = phase_order[_phase_index]
    next_idx = (_phase_index + 1) % len(phase_order)
    next_phase = phase_order[next_idx]

    # 相位自然结束
    if remaining <= 0:
        _phase_index = next_idx
        _phase_elapsed = 0.0
        _planned_duration = plan.green_times.get(next_phase, 15.0)
        _planned_duration = max(_min_green, min(_max_green, _planned_duration))
        _phase_remaining = _planned_duration
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=_planned_duration, reason_code="phase_end",
        )

    # 获取观测
    phase_obs = ego.phases.get(current_phase)
    next_obs = ego.phases.get(next_phase)
    downstream_risk = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
    spillback = ego.downstream_spillback_risk

    cur_demand = _phase_demand(phase_obs)
    nxt_demand = _phase_demand(next_obs)
    elapsed = _phase_elapsed

    # ===== 1. 最小绿灯保护：未达到最小绿灯时间不做任何调整 =====
    if elapsed < _min_green:
        _phase_remaining -= _decision_interval
        _phase_elapsed += _decision_interval
        return PhaseCommand(
            action="hold", next_phase_id=current_phase,
            duration=_decision_interval, reason_code="min_green",
        )

    # ===== 2. 下游溢出保护 - 主动缩短 =====
    if spillback > _downstream_spillback_limit or downstream_risk > _high_downstream_queue:
        if remaining > _decision_interval:
            shorten_amt = min(_max_shorten, remaining - _decision_interval)
            shorten_amt = max(0.0, shorten_amt)
            if shorten_amt > 0:
                new_remaining = remaining - shorten_amt
                _phase_remaining = new_remaining - _decision_interval
                _phase_elapsed += _decision_interval
                return PhaseCommand(
                    action="shorten", next_phase_id=current_phase,
                    duration=new_remaining,
                    reason_code="shorten_spillback_r" + str(int(downstream_risk)),
                )

    # ===== 3. 提前切换 - 当前需求极低且下一相位紧急 =====
    if phase_obs is not None:
        cur_queue = phase_obs.queue
        if (cur_queue < 1.0
            and cur_demand < _low_demand_threshold
            and nxt_demand > _next_urgent_threshold
            and remaining > _decision_interval):
            _phase_index = next_idx
            _phase_elapsed = 0.0
            _planned_duration = plan.green_times.get(next_phase, 15.0)
            _planned_duration = max(_min_green, min(_max_green, _planned_duration))
            _phase_remaining = _planned_duration
            return PhaseCommand(
                action="switch", next_phase_id=next_phase,
                duration=_planned_duration,
                reason_code="early_switch_nd" + str(int(nxt_demand)),
            )

    # ===== 4. 延长 - 当前仍有需求且下游畅通 =====
    if phase_obs is not None and remaining <= _decision_interval:
        if cur_demand > _extend_demand_threshold and downstream_risk < _downstream_risk_limit:
            max_extra = _max_green - elapsed - remaining
            extend_amt = min(_max_extend, max(1.0, cur_demand * 0.3))
            extend_amt = min(extend_amt, max_extra)
            if extend_amt > 0:
                _phase_remaining = remaining + extend_amt - _decision_interval
                _phase_elapsed += _decision_interval
                return PhaseCommand(
                    action="extend", next_phase_id=current_phase,
                    duration=remaining + extend_amt,
                    reason_code="extend_d" + str(int(cur_demand)),
                )

    # ===== 5. 正常推进 =====
    _phase_remaining -= _decision_interval
    _phase_elapsed += _decision_interval
    return PhaseCommand(
        action="hold", next_phase_id=current_phase,
        duration=_decision_interval, reason_code="continuing",
    )


def _reset():
    global _phase_index, _phase_remaining, _phase_elapsed, _current_plan_hash, _planned_duration
    _phase_index = 0
    _phase_remaining = 0.0
    _phase_elapsed = 0.0
    _current_plan_hash = 0
    _planned_duration = 15.0
