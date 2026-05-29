import math

DECISION_INTERVAL = 5.0
MAX_EXTEND = 5.0
MAX_SHORTEN = 5.0
MIN_GREEN = 10.0
MAX_GREEN = 60.0

QUEUE_WEIGHT = 1.0
ARRIVAL_WEIGHT = 0.8
WAIT_WEIGHT = 0.2

DOWNSTREAM_SAFE_THRESHOLD = 15.0
DOWNSTREAM_RISK_THRESHOLD = 20.0

_phase_index = 0
_phase_remaining = 0.0
_current_plan_hash = 0
_phase_elapsed = 0.0


def _plan_hash(plan):
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def _calc_pressure(phase_obs):
    """与CyclePlan完全一致的压力计算方法"""
    if phase_obs is None:
        return 0.1
    s = phase_obs.queue * QUEUE_WEIGHT + phase_obs.predicted_arrival * ARRIVAL_WEIGHT + phase_obs.waiting_time * WAIT_WEIGHT
    return max(s, 0.1)


def _get_downstream_risk(ego):
    risk = 0.0
    if hasattr(ego, 'downstream_queue') and ego.downstream_queue:
        risk = float(sum(ego.downstream_queue.values()))
    if hasattr(ego, 'downstream_spillback_risk'):
        risk += ego.downstream_spillback_risk * 10.0
    return risk


def _get_all_pressures(ego, phase_order):
    """获取所有相位的压力分数"""
    pressures = {}
    for phase_id in phase_order:
        pressures[phase_id] = _calc_pressure(ego.phases.get(phase_id))
    return pressures


def decide(obs, plan):
    global _phase_index, _phase_remaining, _current_plan_hash, _phase_elapsed

    ego = obs.ego
    phase_order = plan.phase_order

    if not phase_order:
        return PhaseCommand(
            action="hold",
            next_phase_id=ego.current_phase_id,
            duration=DECISION_INTERVAL,
            reason_code="no_phases",
        )

    ph = _plan_hash(plan)
    if _current_plan_hash != ph:
        _phase_index = 0
        _current_plan_hash = ph
        _phase_elapsed = 0.0
        first_phase = phase_order[0]
        _phase_remaining = plan.green_times.get(first_phase, MIN_GREEN)
        return PhaseCommand(
            action="switch",
            next_phase_id=first_phase,
            duration=_phase_remaining,
            reason_code="new_plan",
        )

    remaining = _phase_remaining
    elapsed = _phase_elapsed
    current_phase = phase_order[_phase_index]
    num_phases = len(phase_order)

    if remaining <= 0:
        next_idx = (_phase_index + 1) % num_phases
        next_phase = phase_order[next_idx]
        _phase_index = next_idx
        _phase_remaining = plan.green_times.get(next_phase, MIN_GREEN)
        _phase_elapsed = 0.0
        return PhaseCommand(
            action="switch",
            next_phase_id=next_phase,
            duration=_phase_remaining,
            reason_code="phase_end",
        )

    phase_obs = ego.phases.get(current_phase)
    next_idx = (_phase_index + 1) % num_phases
    next_phase = phase_order[next_idx]
    next_phase_obs = ego.phases.get(next_phase)

    # 计算所有相位的压力用于全局决策
    all_pressures = _get_all_pressures(ego, phase_order)
    total_pressure = sum(all_pressures.values())
    
    current_pressure = all_pressures.get(current_phase, 0.1)
    next_pressure = all_pressures.get(next_phase, 0.1)
    downstream_risk = _get_downstream_risk(ego)

    current_queue = phase_obs.queue if phase_obs else 0.0
    current_arrival = phase_obs.predicted_arrival if phase_obs else 0.0
    next_queue = next_phase_obs.queue if next_phase_obs else 0.0
    next_waiting = next_phase_obs.waiting_time if next_phase_obs else 0.0

    upstream_pressure = 0.0
    if hasattr(ego, 'upstream_release_pressure'):
        upstream_pressure = ego.upstream_release_pressure

    min_green_met = elapsed >= MIN_GREEN

    # === 优先处理：下游溢出风险高，动态缩短 ===
    if min_green_met and downstream_risk > DOWNSTREAM_RISK_THRESHOLD:
        if remaining > DECISION_INTERVAL + 1.0:
            risk_severity = min((downstream_risk - DOWNSTREAM_RISK_THRESHOLD) / DOWNSTREAM_RISK_THRESHOLD, 1.0)
            shorten_amount = MAX_SHORTEN * (0.5 + 0.5 * risk_severity)
            shorten_amount = min(shorten_amount, remaining - DECISION_INTERVAL - 1.0)
            shorten_amount = min(shorten_amount, MAX_SHORTEN)
            
            if shorten_amount > 0:
                _phase_remaining = remaining - shorten_amount - DECISION_INTERVAL
                _phase_elapsed += DECISION_INTERVAL
                return PhaseCommand(
                    action="shorten",
                    next_phase_id=current_phase,
                    duration=_phase_remaining + DECISION_INTERVAL,
                    reason_code="shorten_spillback",
                )

    # === 提前切换：当前需求极低且下一相位需求高 ===
    if min_green_met and remaining > DECISION_INTERVAL:
        current_demand = current_queue + current_arrival * 0.5
        next_demand = next_queue + (next_phase_obs.predicted_arrival if next_phase_obs else 0.0) * 0.5
        
        low_demand = current_demand < 1.5
        high_next_demand = next_demand > 3.0 or next_waiting > 30.0
        pressure_ratio = next_pressure / max(current_pressure, 0.1)

        if low_demand and (high_next_demand or pressure_ratio > 3.0):
            next_planned = plan.green_times.get(next_phase, MIN_GREEN)
            _phase_index = next_idx
            _phase_remaining = next_planned
            _phase_elapsed = 0.0
            return PhaseCommand(
                action="switch",
                next_phase_id=next_phase,
                duration=next_planned,
                reason_code="early_switch_low_demand",
            )

    # === 延长：有持续需求且下游安全，考虑全局压力分布 ===
    if min_green_met and 0 < remaining <= DECISION_INTERVAL * 2:
        current_demand = current_queue + current_arrival * 0.5
        has_demand = current_demand > 2.5
        downstream_safe = downstream_risk < DOWNSTREAM_SAFE_THRESHOLD
        can_extend = elapsed + MAX_EXTEND <= MAX_GREEN
        
        # 当前相位压力占比，避免给低优先级相位不合理的延长
        pressure_share = current_pressure / max(total_pressure, 0.1) if total_pressure > 0 else 0
        worth_extending = pressure_share > 0.3 or current_demand > 5.0
        
        # 上游释放压力因子
        upstream_boost = 1.0 + upstream_pressure * 0.15

        if has_demand and downstream_safe and can_extend and worth_extending:
            demand_strength = min(current_demand * upstream_boost / 10.0, 1.0)
            extend_amount = MAX_EXTEND * (0.5 + 0.5 * demand_strength)
            extend_amount = min(extend_amount, MAX_GREEN - elapsed)
            extend_amount = min(extend_amount, MAX_EXTEND)

            if extend_amount > 0:
                _phase_remaining = remaining + extend_amount - DECISION_INTERVAL
                _phase_elapsed += DECISION_INTERVAL
                return PhaseCommand(
                    action="extend",
                    next_phase_id=current_phase,
                    duration=_phase_remaining + DECISION_INTERVAL,
                    reason_code="extend_demand",
                )

    # === 默认 Hold ===
    _phase_remaining -= DECISION_INTERVAL
    _phase_elapsed += DECISION_INTERVAL
    return PhaseCommand(
        action="hold",
        next_phase_id=current_phase,
        duration=DECISION_INTERVAL,
        reason_code="continuing",
    )


def _reset():
    global _phase_index, _phase_remaining, _current_plan_hash, _phase_elapsed
    _phase_index = 0
    _phase_remaining = 0.0
    _current_plan_hash = 0
    _phase_elapsed = 0.0