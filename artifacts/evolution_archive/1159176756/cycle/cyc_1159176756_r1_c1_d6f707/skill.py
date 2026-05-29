def allocate_green_time(phases, raw_scores, effective_green, min_g, max_g):
    green_times = {}
    total_score = sum(raw_scores[p] for p in phases)
    if total_score <= 0:
        total_score = len(phases)
        score_dict = {p: 1.0 for p in phases}
    else:
        score_dict = raw_scores
        
    for p in phases:
        raw_gt = effective_green * (score_dict[p] / total_score)
        green_times[p] = max(min_g, min(max_g, raw_gt))
        
    current_total = sum(green_times.values())
    diff = effective_green - current_total
    
    if abs(diff) > 0.1:
        uncapped_phases = [p for p in phases if green_times[p] < max_g - 0.1 and green_times[p] > min_g + 0.1]
        if uncapped_phases:
            uncapped_score = sum(score_dict[p] for p in uncapped_phases)
            if uncapped_score > 0:
                for p in uncapped_phases:
                    addition = diff * (score_dict[p] / uncapped_score)
                    green_times[p] += addition
                    green_times[p] = max(min_g, min(max_g, green_times[p]))
                    
    current_total = sum(green_times.values())
    diff = effective_green - current_total
    if abs(diff) > 0.01:
        for p in phases:
            if green_times[p] + diff <= max_g + 0.01 and green_times[p] + diff >= min_g - 0.01:
                green_times[p] += diff
                green_times[p] = max(min_g, min(max_g, green_times[p]))
                break
                
    return green_times

def plan(obs):
    ego = obs.ego
    phases = sorted(ego.phases.keys())
    num_phases = len(phases)
    
    if num_phases == 0:
        return CyclePlan(cycle_length=40.0, green_times={}, phase_order=[])
        
    lost_time_per_phase = 5.0
    total_lost_time = num_phases * lost_time_per_phase
    min_g = 10.0
    max_g = 60.0
    
    raw_scores = {}
    total_demand = 0.0
    
    for p in phases:
        phase_obs = ego.phases.get(p)
        if phase_obs is None:
            raw_scores[p] = 0.1
            continue
            
        q = phase_obs.queue
        arr = phase_obs.predicted_arrival
        wt = phase_obs.waiting_time
        
        score = q * 1.0 + arr * 1.5 + wt * 0.2
        raw_scores[p] = max(0.1, score)
        total_demand += q + arr
        
    base_cycle = 60.0 + total_demand * 2.5
    base_cycle = max(60.0, min(180.0, base_cycle))
    
    effective_green = base_cycle - total_lost_time
    
    spillback_risk = 0.0
    if hasattr(ego, 'downstream_spillback_risk'):
        spillback_risk = ego.downstream_spillback_risk
        
    release_pressure = 0.0
    if hasattr(ego, 'upstream_release_pressure'):
        release_pressure = ego.upstream_release_pressure
        
    if spillback_risk > 0:
        effective_green = effective_green * (1.0 - spillback_risk * 0.2)
        
    if release_pressure > 0:
        effective_green = effective_green * (1.0 + release_pressure * 0.1)
        
    max_allowed_green = 180.0 - total_lost_time
    min_allowed_green = 40.0 - total_lost_time
    
    if effective_green > max_allowed_green:
        effective_green = max_allowed_green
    if effective_green < min_allowed_green:
        effective_green = min_allowed_green
        
    if effective_green > num_phases * max_g:
        effective_green = num_phases * max_g
    if effective_green < num_phases * min_g:
        effective_green = num_phases * min_g
        
    green_times = allocate_green_time(phases, raw_scores, effective_green, min_g, max_g)
    
    final_cycle = sum(green_times.values()) + total_lost_time
    
    return CyclePlan(
        cycle_length=final_cycle,
        green_times=green_times,
        phase_order=phases,
        offset_target=0
    )