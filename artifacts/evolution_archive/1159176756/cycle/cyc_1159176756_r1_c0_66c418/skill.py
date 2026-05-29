def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=80.0, green_times={}, phase_order=[])

    min_green = 10.0
    max_green = 60.0
    min_cycle = 40.0
    max_cycle = 180.0
    yellow_time = 3.0
    all_red_time = 2.0
    num_phases = len(green_phases)
    total_loss = num_phases * (yellow_time + all_red_time)

    # 1. 计算各相位的压力分数
    scores = {}
    for gp in green_phases:
        p_obs = ego.phases.get(gp)
        if p_obs is None:
            scores[gp] = 0.1
            continue
        
        # 综合考虑排队长度、预测到达和等待时间
        s = p_obs.queue * 1.0 + p_obs.predicted_arrival * 0.8 + p_obs.waiting_time * 0.2
        scores[gp] = max(s, 0.1)

    # 2. 确定基础周期长度
    total_demand = sum(p.queue for p in ego.phases.values()) + sum(p.predicted_arrival for p in ego.phases.values())
    
    # 根据物理极限约束基础周期
    min_allowed_cycle = total_loss + num_phases * min_green
    max_allowed_cycle = total_loss + num_phases * max_green
    
    if total_demand < 10:
        base_cycle = min_allowed_cycle
    elif total_demand > 60:
        base_cycle = max_allowed_cycle
    else:
        base_cycle = min_allowed_cycle + (max_allowed_cycle - min_allowed_cycle) * (total_demand - 10) / 50.0
        
    # 结合上下游压力因子调节周期
    pressure_factor = 1.0 + ego.upstream_release_pressure * 0.05
    spillback_factor = 1.0 + ego.downstream_spillback_risk * 0.05
    
    target_cycle = base_cycle * pressure_factor * spillback_factor
    target_cycle = max(min_cycle, min(max_cycle, target_cycle))
    
    # 确保目标绿时总和合理
    target_green_total = max(num_phases * min_green, min(num_phases * max_green, target_cycle - total_loss))

    # 3. 绿灯时间带边界约束的迭代分配
    fixed_alloc = {}
    unfixed_phases = set(green_phases)
    total_score = sum(scores.values())
    if total_score == 0:
        total_score = 1.0

    for _ in range(num_phases + 1):
        if not unfixed_phases:
            break
            
        unfixed_score = sum(scores[gp] for gp in unfixed_phases)
        if unfixed_score == 0:
            unfixed_score = 1.0
            
        available_green = target_green_total - sum(fixed_alloc.values())
        if available_green < 0:
            available_green = 0
            
        bounded = False
        next_unfixed = set()
        
        for gp in unfixed_phases:
            alloc = available_green * (scores[gp] / unfixed_score)
            if alloc < min_green:
                fixed_alloc[gp] = min_green
                bounded = True
            elif alloc > max_green:
                fixed_alloc[gp] = max_green
                bounded = True
            else:
                next_unfixed.add(gp)
                
        unfixed_phases = next_unfixed
        
        if not bounded:
            for gp in unfixed_phases:
                fixed_alloc[gp] = available_green * (scores[gp] / unfixed_score)
            break
            
    # 处理极端边界情况未赋值的相位
    for gp in green_phases:
        if gp not in fixed_alloc:
            fixed_alloc[gp] = min_green

    green_times = {}
    for gp in green_phases:
        green_times[gp] = max(min_green, min(max_green, fixed_alloc.get(gp, min_green)))

    # 4. 构造并返回 CyclePlan
    final_cycle = sum(green_times.values()) + total_loss
    
    return CyclePlan(
        cycle_length=final_cycle,
        green_times=green_times,
        phase_order=green_phases,
    )