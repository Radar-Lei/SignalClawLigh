import math


def plan(obs):
    """Improved cycle planner for intersection 1492574988."""
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    
    # Boundary case: no phases
    if not green_phases:
        return CyclePlan(
            cycle_length=80.0, 
            green_times={}, 
            phase_order=[],
            offset_target=0.0
        )
    
    # Constants
    MIN_GREEN = 10.0
    MAX_GREEN = 60.0
    MIN_CYCLE = 40.0
    MAX_CYCLE = 180.0
    YELLOW_TIME = 3.0
    ALL_RED_TIME = 2.0
    
    num_phases = len(green_phases)
    total_loss_time = num_phases * (YELLOW_TIME + ALL_RED_TIME)
    
    # =============================================
    # Part 1: Calculate comprehensive pressure scores
    # =============================================
    scores = {}
    total_queue = 0.0
    total_waiting = 0.0
    
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            scores[gp] = 0.1
            continue
        
        total_queue += phase_obs.queue
        total_waiting += phase_obs.waiting_time
        
        # 1. Queue pressure (linear, core metric)
        queue_component = phase_obs.queue * 1.0
        
        # 2. Waiting time pressure (sqrt growth to prevent extreme bias)
        wait_component = math.sqrt(max(phase_obs.waiting_time, 0.0)) * 1.5
        
        # 3. Predicted arrivals
        arrival_component = phase_obs.predicted_arrival * 0.5
        
        # 4. Saturation factor
        if phase_obs.saturation_flow > 0:
            saturation_ratio = min(
                phase_obs.queue / phase_obs.saturation_flow, 
                2.0
            )
            sat_component = saturation_ratio * 3.0
        else:
            sat_component = 0.0
        
        # 5. Upstream release pressure (positive)
        upstream_component = ego.upstream_release_pressure * 0.3
        
        # 6. Downstream spillback risk (negative)
        spillback_component = ego.downstream_spillback_risk * 1.5
        
        # Combined score
        score = (queue_component 
                + wait_component 
                + arrival_component 
                + sat_component 
                + upstream_component 
                - spillback_component)
        
        scores[gp] = max(score, 0.1)
    
    # =============================================
    # Part 2: Estimate optimal cycle length
    # =============================================
    avg_waiting = total_waiting / max(num_phases, 1)
    
    # Piecewise linear cycle estimation based on queue
    if total_queue <= 0:
        target_cycle = MIN_CYCLE
    elif total_queue <= 5:
        target_cycle = 55.0
    elif total_queue <= 15:
        target_cycle = 55.0 + (total_queue - 5.0) * 2.0
    elif total_queue <= 40:
        target_cycle = 75.0 + (total_queue - 15.0) * 1.2
    elif total_queue <= 80:
        target_cycle = 105.0 + (total_queue - 40.0) * 0.75
    else:
        target_cycle = 135.0 + min((total_queue - 80.0) * 0.3, 25.0)
    
    # Waiting time correction
    if avg_waiting > 60:
        target_cycle *= 1.2
    elif avg_waiting > 30:
        target_cycle *= 1.1
    elif avg_waiting < 5 and total_queue < 3:
        target_cycle *= 0.85
    
    # Downstream spillback risk correction (shorten cycle to reduce accumulation)
    if ego.downstream_spillback_risk > 0.3:
        spillback_factor = 1.0 - 0.1 * min(ego.downstream_spillback_risk, 1.0)
        target_cycle *= spillback_factor
    
    # Upstream release pressure correction (lengthen cycle for backlog)
    if ego.upstream_release_pressure > 1.0:
        upstream_factor = 1.0 + 0.05 * min(ego.upstream_release_pressure, 2.0)
        target_cycle *= upstream_factor
    
    target_cycle = max(MIN_CYCLE, min(MAX_CYCLE, target_cycle))
    
    # =============================================
    # Part 3: Allocate green times
    # =============================================
    available_green = target_cycle - total_loss_time
    
    # Ensure minimum green requirements
    min_total_green = num_phases * MIN_GREEN
    if available_green < min_total_green:
        available_green = min_total_green
        target_cycle = available_green + total_loss_time
        target_cycle = min(target_cycle, MAX_CYCLE)
    
    # Proportional allocation based on scores
    total_score = sum(scores.values())
    green_times = {}
    
    for gp in green_phases:
        if total_score > 0:
            ratio = scores[gp] / total_score
        else:
            ratio = 1.0 / num_phases
        green_times[gp] = available_green * ratio
    
    # =============================================
    # Part 4: Apply green time constraints
    # =============================================
    
    # Step 1: Clamp each phase to [MIN_GREEN, MAX_GREEN]
    for gp in green_phases:
        green_times[gp] = max(MIN_GREEN, min(MAX_GREEN, green_times[gp]))
    
    # Step 2: Check total cycle constraints
    actual_green_sum = sum(green_times.values())
    actual_cycle = actual_green_sum + total_loss_time
    
    # If exceeding max cycle, reduce green times
    if actual_cycle > MAX_CYCLE:
        excess = actual_cycle - MAX_CYCLE
        reducible = [(gp, green_times[gp]) for gp in green_phases if green_times[gp] > MIN_GREEN]
        reducible.sort(key=lambda x: x[1], reverse=True)
        remaining = excess
        for gp, gt in reducible:
            if remaining <= 0:
                break
            reduction = min(gt - MIN_GREEN, remaining)
            green_times[gp] -= reduction
            remaining -= reduction
    
    # If below min cycle, extend green times
    actual_green_sum = sum(green_times.values())
    actual_cycle = actual_green_sum + total_loss_time
    
    if actual_cycle < MIN_CYCLE:
        deficit = MIN_CYCLE - actual_cycle
        extendable = [(gp, scores.get(gp, 0)) for gp in green_phases if green_times[gp] < MAX_GREEN]
        extendable.sort(key=lambda x: x[1], reverse=True)
        remaining = deficit
        for gp, _ in extendable:
            if remaining <= 0:
                break
            extension = min(MAX_GREEN - green_times[gp], remaining)
            green_times[gp] += extension
            remaining -= extension
    
    final_cycle = sum(green_times.values()) + total_loss_time
    final_cycle = max(MIN_CYCLE, min(MAX_CYCLE, final_cycle))
    
    # =============================================
    # Part 5: Calculate offset for coordination
    # =============================================
    offset_target = 0.0
    
    if obs.neighbors and final_cycle > 0:
        total_neighbor_queue = 0.0
        neighbor_count = 0
        
        for neighbor_id, neighbor_obs in obs.neighbors.items():
            if hasattr(neighbor_obs, 'phases') and neighbor_obs.phases:
                neighbor_queue = sum(p.queue for p in neighbor_obs.phases.values())
                total_neighbor_queue += neighbor_queue
                neighbor_count += 1
        
        if neighbor_count > 0:
            # Estimate green wave offset based on travel time
            estimated_travel_time = 20.0
            offset_target = estimated_travel_time % final_cycle
    
    return CyclePlan(
        cycle_length=final_cycle,
        green_times=green_times,
        phase_order=green_phases,
        offset_target=offset_target
    )