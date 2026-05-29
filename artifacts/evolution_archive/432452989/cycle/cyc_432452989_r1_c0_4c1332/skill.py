def plan(obs):
    """Improved deterministic CyclePlannerSkill plan."""
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=40.0, green_times={}, phase_order=[])

    num_phases = len(green_phases)
    min_green = 10.0
    max_green = 60.0
    min_cycle = 40.0
    max_cycle = 180.0
    yellow_time = 3.0
    all_red_time = 2.0
    total_loss_time = num_phases * (yellow_time + all_red_time)

    scores = {}
    total_queue = 0.0
    
    for gp in green_phases:
        p = ego.phases.get(gp)
        if p is None:
            scores[gp] = 0.01
            continue
            
        total_queue += p.queue
        
        # 基于物理需求的绿灯时间计算
        vehicles = p.queue + p.predicted_arrival
        sf = p.saturation_flow if p.saturation_flow > 0 else 1800.0
        demand_time = (vehicles / sf) * 3600.0
        
        # 等待时间补偿防止饥饿
        waiting_bonus = p.waiting_time * 0.2
        
        score = demand_time + waiting_bonus
        scores[gp] = max(score, 0.01)

    # 结合路网状态动态调整周期
    base_cycle = 60.0 + total_queue * 0.5 + ego.upstream_release_pressure * 5.0 - ego.downstream_spillback_risk * 5.0
    target_cycle = max(min_cycle, min(max_cycle, base_cycle))

    # 保证满足最小绿灯和损失时间要求
    min_total_green = num_phases * min_green
    required_cycle = min_total_green + total_loss_time
    target_cycle = max(target_cycle, required_cycle)
    target_cycle = min(target_cycle, max_cycle)

    effective_green = target_cycle - total_loss_time
    effective_green = max(min_total_green, effective_green)

    green_times = {}
    active_phases = list(green_phases)
    current_effective_green = effective_green

    # 迭代分配以保证各相位满足最大最小约束
    for _ in range(5):
        total_score = sum(scores[gp] for gp in active_phases)
        if total_score == 0:
            avg_g = current_effective_green / len(active_phases) if active_phases else 0.0
            for gp in active_phases:
                green_times[gp] = avg_g
            break

        for gp in active_phases:
            green_times[gp] = (scores[gp] / total_score) * current_effective_green

        over_phases = [gp for gp in active_phases if green_times[gp] > max_green]
        under_phases = [gp for gp in active_phases if green_times[gp] < min_green]

        if not over_phases and not under_phases:
            break

        for gp in over_phases:
            green_times[gp] = max_green
            current_effective_green -= max_green
            active_phases.remove(gp)

        for gp in under_phases:
            green_times[gp] = min_green
            current_effective_green -= min_green
            active_phases.remove(gp)

    # 最终约束兜底
    for gp in green_phases:
        if gp not in green_times:
            green_times[gp] = min_green
        green_times[gp] = max(min_green, min(max_green, green_times[gp]))

    final_cycle_length = sum(green_times.values()) + total_loss_time
    final_cycle_length = max(min_cycle, min(max_cycle, final_cycle_length))

    return CyclePlan(
        cycle_length=final_cycle_length,
        green_times=green_times,
        phase_order=green_phases,
    )