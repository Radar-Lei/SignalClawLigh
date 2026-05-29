def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    
    min_green = 10.0
    max_green = 60.0
    min_cycle = 40.0
    max_cycle = 180.0
    
    if not green_phases:
        return CyclePlan(cycle_length=min_cycle, green_times={}, phase_order=[], offset_target=0.0)

    scores = {}
    
    for gp in green_phases:
        p = ego.phases.get(gp)
        if p is None:
            scores[gp] = 0.1
            continue
            
        # 使用 saturation_flow 修正需求，保证物理意义一致性
        flow_ratio = 1800.0 / max(p.saturation_flow, 1.0)
        demand = p.queue + p.predicted_arrival * 0.5
        base_score = demand * flow_ratio
        
        # 考虑等待时间防止饥饿
        hunger = min(p.waiting_time * 0.3, 15.0)
        
        # 考虑上下游影响
        upstream_bonus = ego.upstream_release_pressure * 2.0
        spillback_penalty = ego.downstream_spillback_risk * 2.0
        
        score = max(0.1, base_score + hunger + upstream_bonus - spillback_penalty)
        scores[gp] = score
        
    total_score = sum(scores.values())
    
    # 根据总得分计算目标周期 (总绿灯时间)
    if total_score < 10:
        target_cycle = 50.0
    elif total_score > 80:
        target_cycle = 160.0
    else:
        target_cycle = 50.0 + (total_score - 10.0) / 70.0 * 110.0
        
    target_cycle = max(min_cycle, min(max_cycle, target_cycle))
    
    # 分配绿灯
    green_times = _allocate_green(target_cycle, scores, green_phases, min_green, max_green)
    
    final_cycle = sum(green_times.values())
    final_cycle = max(min_cycle, min(max_cycle, final_cycle))
    
    return CyclePlan(
        cycle_length=final_cycle,
        green_times=green_times,
        phase_order=green_phases,
        offset_target=0.0
    )

def _allocate_green(target_cycle, scores, green_phases, min_green, max_green):
    n_phases = len(green_phases)
    if n_phases == 0:
        return {}
        
    total_score = sum(scores.get(gp, 0.1) for gp in green_phases)
    if total_score <= 0:
        total_score = 1.0
        
    green_times = {}
    
    # 初始按比例分配
    for gp in green_phases:
        s = scores.get(gp, 0.1)
        green_times[gp] = target_cycle * (s / total_score)
        
    # 迭代修正以满足 min/max 约束
    for _ in range(5):
        total_assigned = 0.0
        flexible_phases = []
        
        for gp in green_phases:
            g = green_times[gp]
            if g < min_green:
                green_times[gp] = min_green
                total_assigned += min_green
            elif g > max_green:
                green_times[gp] = max_green
                total_assigned += max_green
            else:
                flexible_phases.append(gp)
                total_assigned += g
                
        diff = target_cycle - total_assigned
        
        if abs(diff) < 0.1 or not flexible_phases:
            break
            
        flex_total_score = sum(scores.get(gp, 0.1) for gp in flexible_phases)
        if flex_total_score > 0:
            for gp in flexible_phases:
                share = diff * (scores.get(gp, 0.1) / flex_total_score)
                green_times[gp] += share
                
    # 最终硬截断
    for gp in green_phases:
        green_times[gp] = max(min_green, min(max_green, green_times[gp]))
        
    return green_times