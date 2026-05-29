import math

# 决策间隔
DECISION_INTERVAL = 5.0

# 约束常量
MIN_GREEN = 10.0
MAX_GREEN = 60.0
MAX_EXTEND = 5.0
MAX_SHORTEN = 5.0

# 阈值
MODERATE_DOWNSTREAM_RISK = 10.0
HIGH_DOWNSTREAM_RISK = 20.0

# 全局状态
_phase_index = 0
_phase_remaining = 0.0
_current_plan_hash = 0
_elapsed_in_phase = 0.0


def _plan_hash(plan):
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def _calc_demand(phase_obs):
    """计算相位需求指标，与CyclePlan的权重计算一致"""
    if phase_obs is None:
        return 0.0
    queue = max(phase_obs.queue, 0.0)
    arrival = max(phase_obs.predicted_arrival, 0.0)
    waiting = max(phase_obs.waiting_time, 0.0)
    return queue * 2.0 + arrival * 1.0 + waiting * 0.5


def decide(obs, plan):
    global _phase_index, _phase_remaining, _current_plan_hash, _elapsed_in_phase

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
        _elapsed_in_phase = 0.0
        first_phase = phase_order[0]
        first_green = plan.green_times.get(first_phase, 15.0)
        _phase_remaining = first_green
        return PhaseCommand(
            action="switch",
            next_phase_id=first_phase,
            duration=first_green,
            reason_code="new_plan",
        )

    remaining = _phase_remaining

    if remaining <= 0:
        next_idx = (_phase_index + 1) % len(phase_order)
        next_phase = phase_order[next_idx]
        _phase_index = next_idx
        _elapsed_in_phase = 0.0
        next_green = plan.green_times.get(next_phase, 15.0)
        _phase_remaining = next_green
        return PhaseCommand(
            action="switch",
            next_phase_id=next_phase,
            duration=next_green,
            reason_code="phase_end",
        )

    current_phase = phase_order[_phase_index]
    phase_obs = ego.phases.get(current_phase)
    current_demand = _calc_demand(phase_obs)

    # 计算下游风险
    downstream_queue_total = 0.0
    if ego.downstream_queue:
        downstream_queue_total = sum(ego.downstream_queue.values())
    spillback_risk = ego.downstream_spillback_risk if ego.downstream_spillback_risk else 0.0
    total_downstream_risk = downstream_queue_total + spillback_risk * 5.0

    # 获取上游压力
    upstream_pressure = ego.upstream_release_pressure if ego.upstream_release_pressure else 0.0

    # 计算下一相位需求
    next_idx = (_phase_index + 1) % len(phase_order)
    next_phase = phase_order[next_idx]
    next_phase_obs = ego.phases.get(next_phase)
    next_demand = _calc_demand(next_phase_obs)

    # 计算所有相位的平均需求
    all_demands = []
    for p in phase_order:
        p_obs = ego.phases.get(p)
        all_demands.append(_calc_demand(p_obs))
    avg_demand = sum(all_demands) / len(all_demands) if len(all_demands) > 0 else 1.0

    # 最小绿灯保护
    if _elapsed_in_phase < MIN_GREEN:
        _phase_remaining -= DECISION_INTERVAL
        _elapsed_in_phase += DECISION_INTERVAL
        return PhaseCommand(
            action="hold",
            next_phase_id=current_phase,
            duration=DECISION_INTERVAL,
            reason_code="min_green",
        )

    # === 微调逻辑 ===

    # 优先级1: 下游溢出保护 - 高优先级
    if total_downstream_risk > HIGH_DOWNSTREAM_RISK and remaining > 5.0:
        shorten_amount = min(MAX_SHORTEN, remaining - 3.0)
        if shorten_amount > 1.0:
            new_duration = max(remaining - shorten_amount, 1.0)
            _phase_remaining = new_duration - DECISION_INTERVAL
            _elapsed_in_phase += DECISION_INTERVAL
            return PhaseCommand(
                action="shorten",
                next_phase_id=current_phase,
                duration=new_duration,
                reason_code="shorten_spillback",
            )

    # 优先级2: 延长逻辑 - 在剩余时间较少时评估
    if 0 < remaining <= 15.0:
        should_extend = False
        extend_score = 0.0

        # 条件A: 当前需求显著高于平均值
        if current_demand > avg_demand * 1.5 and current_demand > 5.0:
            should_extend = True
            extend_score = min(current_demand / max(avg_demand, 1.0), 3.0) / 3.0

        # 条件B: 上游有车队到达
        if upstream_pressure > 3.0 and current_demand > 3.0:
            should_extend = True
            extend_score = max(extend_score, min(upstream_pressure / 5.0, 1.0))

        # 条件C: 下一相位需求极低，切换不划算（黄灯+全红损失5s）
        if next_demand < 2.0 and current_demand > 3.0:
            should_extend = True
            extend_score = max(extend_score, 0.5)

        if should_extend and total_downstream_risk < MODERATE_DOWNSTREAM_RISK:
            extend_amount = MAX_EXTEND * extend_score
            max_possible_extend = MAX_GREEN - _elapsed_in_phase - remaining
            extend_amount = min(extend_amount, max(0.0, max_possible_extend))

            if extend_amount >= 1.0:
                new_duration = remaining + extend_amount
                _phase_remaining = new_duration - DECISION_INTERVAL
                _elapsed_in_phase += DECISION_INTERVAL
                return PhaseCommand(
                    action="extend",
                    next_phase_id=current_phase,
                    duration=new_duration,
                    reason_code="extend_d" + str(int(current_demand)),
                )

    # 优先级3: 提前切换 - 极端不平衡情况
    if remaining > 8.0:
        if current_demand < 2.0 and next_demand > 8.0:
            demand_ratio = next_demand / max(current_demand, 0.1)
            if demand_ratio > 4.0:
                _phase_index = next_idx
                _elapsed_in_phase = 0.0
                next_green = plan.green_times.get(next_phase, 15.0)
                _phase_remaining = next_green
                return PhaseCommand(
                    action="switch",
                    next_phase_id=next_phase,
                    duration=next_green,
                    reason_code="early_switch",
                )

    # 优先级4: 中等下游风险时适度缩短
    if total_downstream_risk > MODERATE_DOWNSTREAM_RISK and current_demand < avg_demand and remaining > 5.0:
        shorten_amount = min(MAX_SHORTEN * 0.6, remaining - 3.0)
        if shorten_amount > 1.0:
            new_duration = max(remaining - shorten_amount, 1.0)
            _phase_remaining = new_duration - DECISION_INTERVAL
            _elapsed_in_phase += DECISION_INTERVAL
            return PhaseCommand(
                action="shorten",
                next_phase_id=current_phase,
                duration=new_duration,
                reason_code="shorten_moderate",
            )

    # 默认：继续持有
    _phase_remaining -= DECISION_INTERVAL
    _elapsed_in_phase += DECISION_INTERVAL
    return PhaseCommand(
        action="hold",
        next_phase_id=current_phase,
        duration=DECISION_INTERVAL,
        reason_code="hold",
    )


def _reset():
    global _phase_index, _phase_remaining, _current_plan_hash, _elapsed_in_phase
    _phase_index = 0
    _phase_remaining = 0.0
    _current_plan_hash = 0
    _elapsed_in_phase = 0.0