from typing import Dict
from signalclaw.core.state import NetworkObservation, CyclePlan

def plan(obs: "NetworkObservation") -> "CyclePlan":
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    
    if not green_phases:
        return CyclePlan(cycle_length=40.0, green_times={}, phase_order=[])
        
    scores = {}
    total_demand = 0.0
    
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            scores[gp] = 0.1
            continue
            
        demand = phase_obs.queue * 1.0 + phase_obs.predicted_arrival * 0.8
        wait_score = phase_obs.waiting_time * 0.3
        
        score = demand + wait_score
        scores[gp] = max(score, 0.1)
        total_demand += demand
        
    # Base cycle calculation bounded by constraints
    cycle = 40.0 + (total_demand / 100.0) * 140.0
    
    # Incorporate upstream and downstream coordination dynamically
    cycle += ego.upstream_release_pressure * 10.0
    cycle -= ego.downstream_spillback_risk * 10.0
    cycle = max(40.0, min(180.0, cycle))
    
    # Iterative allocation to enforce min_green (10.0) and max_green (60.0) constraints
    green_times = {}
    remaining_phases = list(green_phases)
    remaining_cycle = cycle
    
    for _ in range(len(green_phases)):
        if not remaining_phases:
            break
            
        active_scores = {gp: scores.get(gp, 0.1) for gp in remaining_phases}
        total_score = sum(active_scores.values())
        
        temp_times = {}
        if total_score > 0:
            for gp in remaining_phases:
                ratio = active_scores[gp] / total_score
                temp_times[gp] = remaining_cycle * ratio
        else:
            for gp in remaining_phases:
                temp_times[gp] = remaining_cycle / len(remaining_phases)
                
        next_remaining_phases = []
        allocated_cycle = 0.0
        
        for gp in remaining_phases:
            t = temp_times[gp]
            if t <= 10.0:
                green_times[gp] = 10.0
                allocated_cycle += 10.0
            elif t >= 60.0:
                green_times[gp] = 60.0
                allocated_cycle += 60.0
            else:
                next_remaining_phases.append(gp)
                
        if len(next_remaining_phases) == len(remaining_phases):
            for gp in remaining_phases:
                green_times[gp] = temp_times[gp]
            break
        else:
            remaining_cycle -= allocated_cycle
            remaining_phases = next_remaining_phases
            
    for gp in green_phases:
        if gp not in green_times:
            green_times[gp] = 10.0
            
    actual_cycle = sum(green_times.values())
    
    return CyclePlan(
        cycle_length=actual_cycle,
        green_times=green_times,
        phase_order=green_phases,
    )

def _reset():
    pass