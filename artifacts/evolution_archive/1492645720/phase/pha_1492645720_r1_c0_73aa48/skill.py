import math

# 配置常量
_MIN_GREEN = 10.0
_MAX_GREEN = 60.0
_MAX_EXTEND = 5.0
_MAX_SHORTEN = 5.0
_DECISION_INTERVAL = 5.0
_EXTEND_QUEUE_THRESHOLD = 3.0
_EARLY_SWITCH_QUEUE_THRESHOLD = 1.0
_DOWNSTREAM_RISK_THRESHOLD = 10.0

# 状态跟踪
_phase_index = 0
_phase_remaining = 0.0
_current_plan_hash = 0
_elapsed = 0.0


def _plan_hash(plan):
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def _calc_demand(phase_obs):
    if phase_obs is None:
        return 0.1
    return max(phase_obs.queue + phase_obs.predicted_arrival * 0.8 + phase_obs.waiting_time * 0.3, 0.1)


def _downstream_pressure(ego):
    if not ego.downstream_queue:
        return 0.0
    return sum(ego.downstream_queue.values())


def decide(obs, plan):
    global _phase_index, _phase_remaining, _current_plan_hash, _elapsed

    ego = obs.ego
    phase_order = plan.phase_order

    if not phase_order:
        return PhaseCommand(
            action="hold", next_phase_id=ego.current_phase_id,
            duration=_DECISION_INTERVAL, reason_code="no_phases",
        )

    # 新计划检测
    ph = _plan_hash(plan)
    if _current_plan_hash != ph:
        _phase_index = 0
        _current_plan_hash = ph
        _elapsed = 0.0
        first_phase = phase_order[0]
        first_dur = plan.green_times.get(first_phase, 15.0)
        _phase_remaining = first_dur
        return PhaseCommand(
            action="switch", next_phase_id=first_phase,
            duration=first_dur, reason_code="new_plan",
        )

    current_phase = phase_order[_phase_index]
    remaining = _phase_remaining
    _elapsed += _DECISION_INTERVAL

    # 正常相位结束
    if remaining <= 0:
        next_idx = (_phase_index + 1) % len(phase_order)
        next_phase = phase_order[next_idx]
        next_dur = plan.green_times.get(next_phase, 15.0)
        _phase_index = next_idx
        _phase_remaining = next_dur
        _elapsed = 0.0
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=next_dur, reason_code="phase_end",
        )

    # 强制最大绿灯
    if _elapsed >= _MAX_GREEN:
        next_idx = (_phase_index + 1) % len(phase_order)
        next_phase = phase_order[next_idx]
        next_dur = plan.green_times.get(next_phase, 15.0)
        _phase_index = next_idx
        _phase_remaining = next_dur
        _elapsed = 0.0
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=next_dur, reason_code="max_green",
        )

    phase_obs = ego.phases.get(current_phase)
    ds_pressure = _downstream_pressure(ego)

    # 下一相位信息
    next_idx = (_phase_index + 1) % len(phase_order)
    next_phase = phase_order[next_idx]
    next_demand = _calc_demand(ego.phases.get(next_phase))

    # === 扩展逻辑：接近结束且有需求 ===
    if phase_obs is not None and remaining <= _DECISION_INTERVAL * 2:
        q = phase_obs.queue
        arr = phase_obs.predicted_arrival
        ds_ok = ds_pressure < _DOWNSTREAM_RISK_THRESHOLD
        within_max = _elapsed + remaining < _MAX_GREEN

        if ds_ok and within_max:
            # 高队列需求
            if q > _EXTEND_QUEUE_THRESHOLD:
                ext = min(_MAX_EXTEND, _DECISION_INTERVAL)
                _phase_remaining = remaining + ext - _DECISION_INTERVAL
                return PhaseCommand(
                    action="extend", next_phase_id=current_phase,
                    duration=_phase_remaining + _DECISION_INTERVAL,
                    reason_code="ext_q" + str(int(q)),
                )
            # 队列中等但预测有大量到达
            if q > 1.0 and arr > 2.5 and remaining > _DECISION_INTERVAL:
                ext = 3.0
                new_rem = remaining + ext - _DECISION_INTERVAL
                if new_rem > 0:
                    _phase_remaining = new_rem
                    return PhaseCommand(
                        action="extend", next_phase_id=current_phase,
                        duration=_phase_remaining + _DECISION_INTERVAL,
                        reason_code="ext_arr" + str(int(arr)),
                    )

    # === 提前切换（已满足最小绿灯）===
    if _elapsed >= _MIN_GREEN and phase_obs is not None:
        q = phase_obs.queue
        arr = phase_obs.predicted_arrival

        # 当前无需求，下一相位有需求
        if q < _EARLY_SWITCH_QUEUE_THRESHOLD and arr < 1.0 and next_demand > 2.0:
            next_dur = plan.green_times.get(next_phase, 15.0)
            _phase_index = next_idx
            _phase_remaining = next_dur
            _elapsed = 0.0
            return PhaseCommand(
                action="switch", next_phase_id=next_phase,
                duration=next_dur, reason_code="early_q" + str(int(q)),
            )

        # 下游溢出风险极高
        if ds_pressure > _DOWNSTREAM_RISK_THRESHOLD * 2:
            next_dur = plan.green_times.get(next_phase, 15.0)
            _phase_index = next_idx
            _phase_remaining = next_dur
            _elapsed = 0.0
            return PhaseCommand(
                action="switch", next_phase_id=next_phase,
                duration=next_dur, reason_code="spill_" + str(int(ds_pressure)),
            )

    # === 缩短逻辑：需求低 ===
    if phase_obs is not None and _elapsed >= _MIN_GREEN and remaining > _DECISION_INTERVAL * 2:
        q = phase_obs.queue
        if q < 0.5 and next_demand > 1.5:
            shorten = min(_MAX_SHORTEN, remaining - _DECISION_INTERVAL)
            if shorten > 0:
                _phase_remaining = remaining - shorten
                return PhaseCommand(
                    action="shorten", next_phase_id=current_phase,
                    duration=_phase_remaining, reason_code="short_q" + str(round(q, 1)),
                )

    # === 默认保持 ===
    _phase_remaining -= _DECISION_INTERVAL
    return PhaseCommand(
        action="hold", next_phase_id=current_phase,
        duration=_DECISION_INTERVAL, reason_code="hold",
    )


def _reset():
    global _phase_index, _phase_remaining, _current_plan_hash, _elapsed
    _phase_index = 0
    _phase_remaining = 0.0
    _current_plan_hash = 0
    _elapsed = 0.0