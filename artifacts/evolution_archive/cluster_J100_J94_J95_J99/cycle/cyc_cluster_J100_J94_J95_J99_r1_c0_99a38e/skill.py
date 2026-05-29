_last_green_time = {}

def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=60.0, green_times={}, phase_order=[])
    
    num_phases = len(green_phases)
    lost_time_per_phase = 5.0
    total_lost_time = lost_time_per_phase * num_phases
    
    min_cycle = 40.0
    max_cycle = 180.0
    min_green = 10.0
    max_green = 60.0
    
    total_queue = sum(p.queue for p in ego.phases.values())
    total_arrival = sum(p.predicted_arrival for p in ego.phases.values())
    
    base_cycle = 60.0
    if total_queue + total_arrival > 60:
        base_cycle = 140.0
    elif total_queue + total_arrival > 30:
        base_cycle = 100.0
        
    base_cycle += min(ego.upstream_release_pressure * 2.0, 20.0)
    base_cycle -= min(ego.downstream_spillback_risk * 3.0, 30.0)
    
    min_req_cycle = min_green * num_phases + total_lost_time
    cycle_length = max(min_cycle, min(max_cycle, base_cycle))
    cycle_length = max(cycle_length, min_req_cycle)
    
    effective_green = max(cycle_length - total_lost_time, min_green * num_phases)
    
    scores = {}
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            scores[gp] = 0.01
            continue
        demand = phase_obs.queue + phase_obs.predicted_arrival
        waiting = phase_obs.waiting_time
        
        hunger_time = obs.timestamp - _last_green_time.get(gp, obs.timestamp)
        hunger_bonus = min(hunger_time * 0.3, 10.0)
        
        score = demand * 1.0 + waiting * 0.2 + hunger_bonus
        scores[gp] = max(score, 0.01)
        
    green_times = {}
    remaining_phases = set(green_phases)
    remaining_green = effective_green
    
    for _ in range(num_phases):
        if not remaining_phases:
            break
        sum_scores = sum(scores[p] for p in remaining_phases)
        if sum_scores <= 0:
            for p in remaining_phases:
                green_times[p] = remaining_green / len(remaining_phases)
            break
        
        current_ideal = {p: remaining_green * (scores[p] / sum_scores) for p in remaining_phases}
        
        fixed_in_this_round = {}
        for p in remaining_phases:
            if current_ideal[p] < min_green:
                fixed_in_this_round[p] = min_green
            elif current_ideal[p] > max_green:
                fixed_in_this_round[p] = max_green
                
        if not fixed_in_this_round:
            for p in remaining_phases:
                green_times[p] = current_ideal[p]
            break
        else:
            for p, t in fixed_in_this_round.items():
                green_times[p] = t
                remaining_green -= t
            remaining_phases = remaining_phases - set(fixed_in_this_round.keys())
            
    for p in remaining_phases:
        if p not in green_times:
            green_times[p] = min_green
            remaining_green -= min_green
            
    final_cycle_length = sum(green_times.values()) + total_lost_time
    
    if final_cycle_length > max_cycle:
        excess = final_cycle_length - max_cycle
        sorted_phases = sorted(green_phases, key=lambda p: scores[p])
        for p in sorted_phases:
            if excess <= 0: break
            reducible = green_times[p] - min_green
            reduce = min(reducible, excess)
            green_times[p] -= reduce
            excess -= reduce
        final_cycle_length = sum(green_times.values()) + total_lost_time
        
    elif final_cycle_length < min_cycle:
        deficit = min_cycle - final_cycle_length
        sorted_phases = sorted(green_phases, key=lambda p: scores[p], reverse=True)
        for p in sorted_phases:
            if deficit <= 0: break
            increasable = max_green - green_times[p]
            increase = min(increasable, deficit)
            green_times[p] += increase
            deficit -= increase
        final_cycle_length = sum(green_times.values()) + total_lost_time
        
    for p in green_phases:
        _last_green_time[p] = obs.timestamp
        
    return CyclePlan(
        cycle_length=final_cycle_length,
        green_times=green_times,
        phase_order=green_phases,
    )