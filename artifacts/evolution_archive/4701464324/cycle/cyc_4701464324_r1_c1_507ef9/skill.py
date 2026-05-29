def plan(obs):
    ego = obs.ego
    phases = sorted(ego.phases.keys())
    
    if not phases:
        return CyclePlan(cycle_length=40.0, green_times={}, phase_order=[])
        
    scores = {}
    total_demand = 0.0
    
    for gp in phases:
        p = ego.phases.get(gp)
        if p is None:
            scores[gp] = 0.1
            continue
            
        q = p.queue
        arr = p.predicted_arrival
        wt = p.waiting_time
        
        demand = q + arr * 1.5
        urgency = wt * 0.5
        
        score = demand + urgency
        scores[gp] = max(score, 0.1)
        
        total_demand += q + arr
        
    # Determine base target cycle length based on total demand
    if total_demand < 5.0:
        target_cycle = 40.0
    elif total_demand > 80.0:
        target_cycle = 180.0
    else:
        target_cycle = 40.0 + (180.0 - 40.0) * (total_demand - 5.0) / 75.0
        
    # Adjust for downstream spillback risk (reduce cycle to prevent overflow)
    spillback_risk = ego.downstream_spillback_risk
    target_cycle = target_cycle - spillback_risk * 15.0
    
    # Adjust for upstream release pressure (increase cycle to clear queues)
    upstream_pressure = ego.upstream_release_pressure
    target_cycle = target_cycle + upstream_pressure * 15.0
    
    target_cycle = max(40.0, min(180.0, target_cycle))
    
    # Iterative allocation to strictly satisfy min/max green times
    green_times = {}
    current_phases = list(phases)
    current_weights = {p: scores.get(p, 0.1) for p in current_phases}
    current_target = target_cycle
    
    for _ in range(10):
        total_w = sum(current_weights.get(p, 0.1) for p in current_phases)
        if total_w == 0:
            total_w = 1.0
            
        allocated = {}
        for gp in current_phases:
            allocated[gp] = current_target * (current_weights.get(gp, 0.1) / total_w)
            
        next_phases = []
        next_weights = {}
        fixed_sum = 0.0
        
        for gp in current_phases:
            if allocated[gp] <= 10.0:
                green_times[gp] = 10.0
                fixed_sum += 10.0
            elif allocated[gp] >= 60.0:
                green_times[gp] = 60.0
                fixed_sum += 60.0
            else:
                next_phases.append(gp)
                next_weights[gp] = current_weights.get(gp, 0.1)
                
        if len(next_phases) == len(current_phases):
            for gp in current_phases:
                green_times[gp] = max(10.0, min(60.0, allocated[gp]))
            break
            
        if not next_phases:
            break
            
        current_phases = next_phases
        current_weights = next_weights
        current_target = max(0.0, current_target - fixed_sum)
        
    # Guarantee all phases have a green time
    for gp in phases:
        if gp not in green_times:
            green_times[gp] = 10.0
            
    # Proportionally reduce green times if their sum exceeds max_cycle
    total_green = sum(green_times.values())
    if total_green > 180.0:
        excess = total_green - 180.0
        reducible = sum(v - 10.0 for v in green_times.values())
        if reducible > 0:
            for gp in phases:
                reduce_by = excess * ((green_times[gp] - 10.0) / reducible)
                green_times[gp] = max(10.0, green_times[gp] - reduce_by)
                
    final_cycle = sum(green_times.values())
    final_cycle = max(40.0, min(180.0, final_cycle))
    
    return CyclePlan(
        cycle_length=final_cycle,
        green_times=green_times,
        phase_order=phases
    )