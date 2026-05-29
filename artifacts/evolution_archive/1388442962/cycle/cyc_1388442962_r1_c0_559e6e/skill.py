def plan(obs):
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
    
    weights = {}
    total_queue = 0.0
    
    for gp in green_phases:
        p = ego.phases.get(gp)
        if p is None:
            weights[gp] = 1.0
            continue
        queue = max(p.queue, 0.0)
        arrival = max(p.predicted_arrival, 0.0)
        waiting = max(p.waiting_time, 0.0)
        
        # 综合考虑排队、到达预测与等待时间
        w = queue * 2.0 + arrival * 1.0 + waiting * 0.5
        weights[gp] = max(w, 0.1)
        total_queue += queue
        
    # 基于总排队的基准周期计算
    base_cycle = min_cycle + (max_cycle - min_cycle) * min(total_queue / 60.0, 1.0)
    
    # 结合上下游路网压力进行微调
    upstream_pressure = ego.upstream_release_pressure if ego.upstream_release_pressure else 0.0
    downstream_risk = ego.downstream_spillback_risk if ego.downstream_spillback_risk else 0.0
    
    cycle_length = base_cycle + upstream_pressure * 3.0 - downstream_risk * 3.0
    cycle_length = max(min_cycle, min(max_cycle, cycle_length))
    
    # 计算目标总绿灯时间
    total_green_target = cycle_length - total_lost_time
    min_total_green = num_phases * min_green
    total_green_target = max(total_green_target, min_total_green)
    max_total_green = max_cycle - total_lost_time
    total_green_target = min(total_green_target, max_total_green)
    
    sum_weights = sum(weights.values())
    green_times = {}
    
    # 按权重初始分配绿灯时间
    for gp in green_phases:
        if sum_weights > 0:
            gt = (weights[gp] / sum_weights) * total_green_target
        else:
            gt = total_green_target / num_phases
        green_times[gp] = max(min_green, min(max_green, gt))
        
    # 迭代平衡各相位绿灯，以满足最小/最大绿灯限制及目标总时长
    for _ in range(10):
        current_total_green = sum(green_times.values())
        diff = total_green_target - current_total_green
        
        if abs(diff) < 0.1:
            break
            
        adjustable_phases = []
        for gp in green_phases:
            gt = green_times[gp]
            if diff > 0 and gt < max_green:
                adjustable_phases.append(gp)
            elif diff < 0 and gt > min_green:
                adjustable_phases.append(gp)
                
        if not adjustable_phases:
            break
            
        sum_adj_weights = sum(weights[gp] for gp in adjustable_phases)
        if sum_adj_weights <= 0:
            sum_adj_weights = float(len(adjustable_phases))
            
        for gp in adjustable_phases:
            delta = (weights[gp] / sum_adj_weights) * diff
            new_gt = green_times[gp] + delta
            green_times[gp] = max(min_green, min(max_green, new_gt))
            
    # 最终周期等于实际绿灯总和加上损失时间
    final_cycle_length = sum(green_times.values()) + total_lost_time
    
    return CyclePlan(
        cycle_length=final_cycle_length,
        green_times=green_times,
        phase_order=green_phases
    )