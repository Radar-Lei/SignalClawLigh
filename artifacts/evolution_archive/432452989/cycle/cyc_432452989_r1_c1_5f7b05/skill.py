def plan(obs: "NetworkObservation") -> "CyclePlan":
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=40.0, green_times={}, phase_order=[])

    min_green = 10.0
    max_green = 60.0
    min_cycle = 40.0
    max_cycle = 180.0
    yellow_time = 3.0
    all_red_time = 2.0
    
    num_phases = len(green_phases)
    total_lost_time = num_phases * (yellow_time + all_red_time)
    max_total_green = max(0.0, max_cycle - total_lost_time)
    min_total_green = num_phases * min_green
    
    demands = {}
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            demands[gp] = 1.0
            continue
            
        local_pressure = phase_obs.queue
        arrival_prediction = phase_obs.waiting_time * 0.3 + phase_obs.predicted_arrival * 1.0
        
        # 综合本地排队、到达预测、上下游压力计算该相位需求
        d = 1.0 * local_pressure + 0.5 * arrival_prediction
        d += ego.upstream_release_pressure * 1.0
        d -= ego.downstream_spillback_risk * 1.5
        
        demands[gp] = max(d, 0.1)

    total_demand = sum(demands.values())
    
    total_queue = sum(ego.phases[gp].queue for gp in green_phases)
    # 估算目标总绿灯时间：处理当前排队并附加基础缓冲时间
    G_target = total_queue * 2.0 + num_phases * 5.0
    G_target = max(min_total_green, min(max_total_green, G_target))

    green_times = {}
    if total_demand > 0:
        for gp in green_phases:
            gt = G_target * (demands[gp] / total_demand)
            green_times[gp] = max(min_green, min(max_green, gt))
    else:
        for gp in green_phases:
            green_times[gp] = G_target / num_phases

    G_actual = sum(green_times.values())

    # 边界修正：如果因为个别相位达到 max_green 导致总绿灯时间超出允许上限，则按比例压缩
    if G_actual > max_total_green:
        excess = G_actual - max_total_green
        reducible = {}
        total_reducible = 0.0
        for gp, gt in green_times.items():
            if gt > min_green:
                reducible[gp] = gt - min_green
                total_reducible += reducible[gp]
        
        if total_reducible > 0:
            for gp, red in reducible.items():
                green_times[gp] -= excess * (red / total_reducible)
                green_times[gp] = max(min_green, green_times[gp])
        
        G_actual = sum(green_times.values())

    cycle_length = G_actual + total_lost_time
    cycle_length = max(min_cycle, min(max_cycle, cycle_length))

    plan_obj = CyclePlan(
        cycle_length=cycle_length,
        green_times=green_times,
        phase_order=green_phases,
    )
    return plan_obj