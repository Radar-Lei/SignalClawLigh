def _allocate_green(phases, pressures, available_green, min_green, max_green):
    green_times = {}
    total_p = sum(pressures.get(p, 0.0) for p in phases)
    
    if total_p <= 0:
        for p in phases:
            green_times[p] = available_green / max(len(phases), 1)
    else:
        for p in phases:
            green_times[p] = available_green * (pressures.get(p, 0.0) / total_p)
            
    for _ in range(10):
        clipped = {}
        free_phases = []
        fixed_sum = 0.0
        for p in phases:
            if green_times[p] <= min_green:
                clipped[p] = min_green
                fixed_sum += min_green
            elif green_times[p] >= max_green:
                clipped[p] = max_green
                fixed_sum += max_green
            else:
                clipped[p] = green_times[p]
                free_phases.append(p)
                
        if not free_phases:
            green_times = clipped
            break
            
        current_free_sum = sum(clipped[p] for p in free_phases)
        target_free_sum = available_green - fixed_sum
        
        if target_free_sum <= 0 or current_free_sum <= 0:
            green_times = clipped
            break
            
        scale = target_free_sum / current_free_sum
        for p in free_phases:
            clipped[p] *= scale
            
        green_times = clipped

    for p in phases:
        green_times[p] = max(min_green, min(max_green, green_times[p]))
        
    return green_times

def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=40.0, green_times={}, phase_order=[], offset_target=0.0)
        
    loss_per_phase = 3.0 + 2.0
    total_loss = len(green_phases) * loss_per_phase
    min_green = 10.0
    max_green = 60.0
    
    pressures = {}
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            pressures[gp] = 0.1
            continue
            
        local_pressure = phase_obs.queue
        arrival_prediction = phase_obs.waiting_time * 0.3 + phase_obs.predicted_arrival
        
        downstream_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
        n_downstream = max(len(ego.downstream_queue), 1)
        avg_downstream = downstream_total / n_downstream
        spillback_risk = max(0, avg_downstream - 5.0) * 2.0
        
        upstream_bonus = ego.upstream_release_pressure * 1.5
        
        score = (
            1.5 * local_pressure
            + 1.0 * arrival_prediction
            - 1.0 * spillback_risk
            + 0.8 * upstream_bonus
        )
        pressures[gp] = max(score, 0.1)
        
    total_q = sum(p.queue for p in ego.phases.values())
    if total_q < 5:
        target_cycle = 40.0
    elif total_q > 50:
        target_cycle = 180.0
    else:
        target_cycle = 40.0 + 140.0 * (total_q - 5.0) / 45.0
        
    min_feasible_cycle = total_loss + len(green_phases) * min_green
    target_cycle = max(target_cycle, min_feasible_cycle)
    target_cycle = min(target_cycle, 180.0)
    
    available_green = target_cycle - total_loss
    
    green_times = _allocate_green(green_phases, pressures, available_green, min_green, max_green)
    
    final_cycle_length = sum(green_times.values()) + total_loss
    final_cycle_length = max(40.0, min(180.0, final_cycle_length))
    
    return CyclePlan(
        cycle_length=final_cycle_length,
        green_times=green_times,
        phase_order=green_phases,
        offset_target=0.0
    )