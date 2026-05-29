import math

_MIN_GREEN = 10.0
_MAX_GREEN = 60.0
_MIN_CYCLE = 40.0
_MAX_CYCLE = 180.0
_YELLOW = 3.0
_ALL_RED = 2.0
_LOST_PER_PHASE = _YELLOW + _ALL_RED


def _compute_phase_score(phase_obs, spillback_risk, upstream_pressure):
    """计算单个相位的流量比和综合需求分数"""
    if phase_obs is None:
        return 0.01, 1.0

    queue = max(phase_obs.queue, 0.0)
    waiting = max(phase_obs.waiting_time, 0.0)
    arrival = max(phase_obs.predicted_arrival, 0.0)
    sat_flow = max(phase_obs.saturation_flow, 1.0)

    # 流量比 (用于 Webster 公式和绿灯分配)
    effective_demand = queue + arrival
    y = min(effective_demand / sat_flow, 0.95)
    y = max(y, 0.01)

    # 综合需求分数
    q_part = queue * 1.0
    w_part = min(waiting, 120.0) / 25.0
    a_part = arrival * 0.4
    s_part = max(0.0, waiting - 40.0) * 0.1 if waiting > 40.0 else 0.0

    base_score = q_part + w_part + a_part + s_part

    # 下游溢出风险惩罚
    if spillback_risk > 0.3:
        base_score *= max(0.4, 1.0 - spillback_risk * 0.6)

    # 上游释放压力加成
    base_score *= (1.0 + min(upstream_pressure * 0.2, 2.0))

    return y, max(base_score, 0.5)


def _allocate_green_times(green_phases, target_total, combined_scores, min_g, max_g):
    """按比例分配绿灯时间，迭代满足约束"""
    n = len(green_phases)
    if n == 0:
        return {}

    total_score = sum(combined_scores[gp] for gp in green_phases)

    result = {}
    for gp in green_phases:
        if total_score > 0:
            gt = target_total * combined_scores[gp] / total_score
        else:
            gt = target_total / n
        result[gp] = gt

    for gp in green_phases:
        result[gp] = max(min_g, min(max_g, result[gp]))

    for _iter in range(10):
        current_total = sum(result[gp] for gp in green_phases)
        diff = target_total - current_total

        if abs(diff) < 0.5:
            break

        if diff > 0:
            adjustable = [gp for gp in green_phases if result[gp] < max_g - 0.01]
        else:
            adjustable = [gp for gp in green_phases if result[gp] > min_g + 0.01]

        if not adjustable:
            break

        share = diff / len(adjustable)
        for gp in adjustable:
            new_val = result[gp] + share
            result[gp] = max(min_g, min(max_g, new_val))

    return result


def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())

    if not green_phases:
        return CyclePlan(cycle_length=80.0, green_times={}, phase_order=[])

    n_phases = len(green_phases)
    total_lost = n_phases * _LOST_PER_PHASE

    spillback_risk = ego.downstream_spillback_risk
    upstream_pressure = ego.upstream_release_pressure

    flow_ratios = {}
    demand_scores = {}

    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        y, score = _compute_phase_score(phase_obs, spillback_risk, upstream_pressure)
        flow_ratios[gp] = y
        demand_scores[gp] = score

    # 混合评分: 65% 需求分数 + 35% 流量比 (归一化)
    combined = {}
    total_demand = sum(demand_scores[gp] for gp in green_phases)
    total_flow = sum(flow_ratios[gp] for gp in green_phases)

    for gp in green_phases:
        d_norm = demand_scores[gp] / max(total_demand, 0.01)
        f_norm = flow_ratios[gp] / max(total_flow, 0.01)
        combined[gp] = 0.65 * d_norm + 0.35 * f_norm

    # Webster 最优周期
    Y = min(sum(flow_ratios[gp] for gp in green_phases), 0.95)

    if Y > 0.01:
        webster_cycle = (1.5 * total_lost + 5.0) / (1.0 - Y)
        ref_green = webster_cycle - total_lost
    else:
        ref_green = 60.0

    # 拥堵评估
    total_queue = 0.0
    total_waiting = 0.0
    for p in ego.phases.values():
        total_queue += max(p.queue, 0.0)
        total_waiting += max(p.waiting_time, 0.0)

    avg_queue = total_queue / max(n_phases, 1)
    avg_waiting = total_waiting / max(n_phases, 1)

    if avg_queue < 2.0:
        congestion_factor = 0.8
    elif avg_queue > 15.0:
        congestion_factor = 1.2
    else:
        congestion_factor = 0.8 + 0.4 * (avg_queue - 2.0) / 13.0

    if avg_waiting > 50.0:
        wait_factor = 1.05 + min(avg_waiting - 50.0, 70.0) * 0.003
        congestion_factor = max(congestion_factor, wait_factor)

    target_green = ref_green * congestion_factor
    target_green = max(_MIN_CYCLE, min(_MAX_CYCLE, target_green))

    green_times = _allocate_green_times(
        green_phases, target_green, combined, _MIN_GREEN, _MAX_GREEN
    )

    actual_cycle = sum(green_times[gp] for gp in green_phases)
    actual_cycle = max(_MIN_CYCLE, min(_MAX_CYCLE, actual_cycle))

    return CyclePlan(
        cycle_length=actual_cycle,
        green_times=green_times,
        phase_order=green_phases,
    )