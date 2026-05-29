def plan(obs):
    ego = obs.ego
    phases = ego.phases
    green_phases = sorted(phases.keys())
    
    if not green_phases:
        return CyclePlan(cycle_length=80.0, green_times={}, phase_order=[], offset_target=0.0)

    n_phases = len(green_phases)
    total_loss = n_phases * 5.0  # 3.0s yellow + 2.0s all_red

    total_queue = 0.0
    for ph in green_phases:
        total_queue += phases[ph].queue

    min_cycle = 40.0
    max_cycle = 180.0
    
    # Base cycle calculation based on total queue
    if total_queue < 5.0:
        base_cycle = 60.0
    elif total_queue > 50.0:
        base_cycle = 150.0
    else:
        base_cycle = 60.0 + 90.0 * (total_queue - 5.0) / 45.0

    # Adjust cycle based on downstream spillback and upstream pressure
    spill_factor = 1.0 - min(ego.downstream_spillback_risk, 3.0) * 0.1
    base_cycle *= max(0.7, spill_factor)
    
    pressure_factor = 1.0 + min(ego.upstream_release_pressure, 4.0) * 0.1
    base_cycle *= min(1.3, pressure_factor)
    
    target_cycle = max(min_cycle, min(max_cycle, base_cycle))
    
    target_green_total = target_cycle - total_loss
    
    min_green_sum = 0.0
    max_green_sum = 0.0
    for ph in green_phases:
        min_green_sum += phases[ph].min_green
        max_green_sum += phases[ph].max_green
        
    target_green_total = max(min_green_sum, min(max_green_sum, target_green_total))

    # Calculate scores for green time distribution
    scores = {}
    for ph in green_phases:
        p = phases[ph]
        s = p.queue * 1.0 + p.waiting_time * 0.4 + p.predicted_arrival * 0.8
        scores[ph] = max(s, 0.1)
        
    total_score = 0.0
    for val in scores.values():
        total_score += val
    
    green_times = {}
    for ph in green_phases:
        if total_score > 0:
            green_times[ph] = target_green_total * (scores[ph] / total_score)
        else:
            green_times[ph] = target_green_total / n_phases

    # Iterative adjustment to satisfy min/max green constraints
    for _ in range(10):
        current_total = 0.0
        for ph in green_phases:
            current_total += green_times[ph]
            
        if current_total > 0:
            scale = target_green_total / current_total
            for ph in green_phases:
                green_times[ph] *= scale
        else:
            for ph in green_phases:
                green_times[ph] = target_green_total / n_phases
                
        clipped_total = 0.0
        unclipped = []
        for ph in green_phases:
            min_g = phases[ph].min_green
            max_g = phases[ph].max_green
            if green_times[ph] < min_g:
                green_times[ph] = min_g
                clipped_total += min_g
            elif green_times[ph] > max_g:
                green_times[ph] = max_g
                clipped_total += max_g
            else:
                unclipped.append(ph)
                
        if unclipped:
            remaining = target_green_total - clipped_total
            unclipped_sum = 0.0
            for ph in unclipped:
                unclipped_sum += green_times[ph]
                
            if unclipped_sum > 0:
                for ph in unclipped:
                    green_times[ph] = remaining * (green_times[ph] / unclipped_sum)
            else:
                for ph in unclipped:
                    green_times[ph] = remaining / len(unclipped)

    # Final pass to guarantee constraints
    final_green_total = 0.0
    for ph in green_phases:
        gt = green_times[ph]
        gt = max(phases[ph].min_green, min(phases[ph].max_green, gt))
        green_times[ph] = gt
        final_green_total += gt

    final_cycle = final_green_total + total_loss
    final_cycle = max(min_cycle, min(max_cycle, final_cycle))

    return CyclePlan(
        cycle_length=final_cycle,
        green_times=green_times,
        phase_order=green_phases,
        offset_target=0.0
    )