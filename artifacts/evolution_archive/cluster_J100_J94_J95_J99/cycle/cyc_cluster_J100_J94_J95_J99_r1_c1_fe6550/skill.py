def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    num_phases = len(green_phases)
    
    if num_phases == 0:
        return CyclePlan(cycle_length=80.0, green_times={}, phase_order=[])

    yellow_time = 3.0
    all_red_time = 2.0
    lost_time_per_phase = yellow_time + all_red_time
    total_lost_time = num_phases * lost_time_per_phase
    
    min_green = 10.0
    max_green = 60.0
    max_cycle = 180.0
    
    scores = {}
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            scores[gp] = 0.1
            continue
            
        local_pressure = phase_obs.queue * 1.2 + phase_obs.predicted_arrival * 0.8
        hunger_bonus = phase_obs.waiting_time * 0.5
        
        downstream_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
        n_downstream = max(len(ego.downstream_queue), 1)
        avg_downstream = downstream_total / n_downstream
        spillback_risk = max(0, avg_downstream - 5.0) * 2.0 + ego.downstream_spillback_risk * 2.5
        upstream_pressure = ego.upstream_release_pressure * 1.0
        
        score = local_pressure + hunger_bonus - spillback_risk + upstream_pressure
        scores[gp] = max(score, 0.1)
        
    total_score = sum(scores.values())
    
    total_queue = sum(p.queue for p in ego.phases.values())
    total_waiting = sum(p.waiting_time for p in ego.phases.values())
    congestion_index = total_queue * 1.0 + total_waiting * 0.1
    
    min_feasible_cycle = num_phases * min_green + total_lost_time
    
    if congestion_index < 10.0:
        target_cycle = min_feasible_cycle
    elif congestion_index > 200.0:
        target_cycle = max_cycle
    else:
        target_cycle = min_feasible_cycle + (max_cycle - min_feasible_cycle) * (congestion_index - 10.0) / 190.0
        
    target_cycle = max(min_feasible_cycle, min(max_cycle, target_cycle))
    
    total_green_time = target_cycle - total_lost_time
    
    green_times = {}
    allocated_green = 0.0
    
    for gp in green_phases:
        if total_score > 0:
            gt = total_green_time * (scores[gp] / total_score)
        else:
            gt = total_green_time / num_phases
            
        gt = max(min_green, min(max_green, gt))
        green_times[gp] = gt
        allocated_green += gt
        
    actual_cycle = allocated_green + total_lost_time
    
    if actual_cycle > max_cycle:
        scale = (max_cycle - total_lost_time) / allocated_green
        allocated_green = 0.0
        for gp in green_phases:
            gt = green_times[gp] * scale
            gt = max(min_green, min(max_green, gt))
            green_times[gp] = gt
            allocated_green += gt
        actual_cycle = allocated_green + total_lost_time

    return CyclePlan(
        cycle_length=actual_cycle,
        green_times=green_times,
        phase_order=green_phases,
    )