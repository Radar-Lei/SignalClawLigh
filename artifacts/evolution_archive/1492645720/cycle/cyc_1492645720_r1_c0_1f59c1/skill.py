def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    
    min_green = 10.0
    max_green = 60.0
    min_cycle = 40.0
    max_cycle = 180.0

    if not green_phases:
        return CyclePlan(cycle_length=min_cycle, green_times={}, phase_order=[], offset_target=0.0)

    weights = {}
    total_queue = 0.0
    total_predicted = 0.0
    
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            weights[gp] = 1.0
            continue
        
        queue = phase_obs.queue
        predicted = phase_obs.predicted_arrival
        waiting = phase_obs.waiting_time
        
        total_queue += queue
        total_predicted += predicted
        
        upstream_pressure = ego.upstream_release_pressure
        if not upstream_pressure:
            upstream_pressure = 0.0
            
        spillback_risk = ego.downstream_spillback_risk
        if not spillback_risk:
            spillback_risk = 0.0
            
        w = queue * 1.0 + predicted * 0.5 + waiting * 0.1 + upstream_pressure * 0.2 + 1.0 - spillback_risk * 2.0
        weights[gp] = max(0.1, w)

    demand = total_queue + total_predicted
    demand_ratio = max(0.0, min(1.0, (demand - 5.0) / 55.0))
    target_cycle = 40.0 + 140.0 * demand_ratio

    total_weight = sum(weights.values())
    green_times = {}
    
    for gp in green_phases:
        if total_weight > 0:
            gt = target_cycle * (weights[gp] / total_weight)
        else:
            gt = target_cycle / len(green_phases)
        
        green_times[gp] = max(min_green, min(max_green, gt))

    for _ in range(5):
        actual_cycle = sum(green_times.values())
        if actual_cycle > max_cycle:
            reducible_phases = [gp for gp in green_phases if green_times[gp] > min_green]
            if not reducible_phases:
                break
            excess = actual_cycle - max_cycle
            total_reducible = sum(green_times[gp] - min_green for gp in reducible_phases)
            if total_reducible > 0:
                reduce_ratio = excess / total_reducible
                for gp in reducible_phases:
                    reduction = (green_times[gp] - min_green) * reduce_ratio
                    green_times[gp] = max(min_green, green_times[gp] - reduction)
            else:
                break
        elif actual_cycle < min_cycle:
            increasible_phases = [gp for gp in green_phases if green_times[gp] < max_green]
            if not increasible_phases:
                break
            deficit = min_cycle - actual_cycle
            total_increasible = sum(max_green - green_times[gp] for gp in increasible_phases)
            if total_increasible > 0:
                increase_ratio = deficit / total_increasible
                for gp in increasible_phases:
                    increase = (max_green - green_times[gp]) * increase_ratio
                    green_times[gp] = min(max_green, green_times[gp] + increase)
            else:
                break
        else:
            break

    final_cycle = sum(green_times.values())

    return CyclePlan(
        cycle_length=final_cycle,
        green_times=green_times,
        phase_order=green_phases,
        offset_target=0.0
    )