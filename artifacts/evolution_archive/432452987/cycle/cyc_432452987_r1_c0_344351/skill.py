def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=80.0, green_times={}, phase_order=[])

    yellow_time = 3.0
    all_red_time = 2.0
    min_green = 10.0
    max_green = 60.0
    min_cycle = 40.0
    max_cycle = 180.0

    total_loss_time = len(green_phases) * (yellow_time + all_red_time)

    total_queue = sum(p.queue for p in ego.phases.values())

    base_cycle = 80.0
    if total_queue < 5:
        cycle_length = base_cycle * 0.8
    elif total_queue > 50:
        cycle_length = base_cycle * 1.4
    else:
        cycle_length = base_cycle * (0.8 + 0.6 * (total_queue - 5) / 45.0)

    cycle_length = max(min_cycle, min(max_cycle, cycle_length))

    if ego.downstream_spillback_risk > 0.5:
        cycle_length = max(min_cycle, cycle_length * 0.9)

    min_effective_green = len(green_phases) * min_green
    if cycle_length - total_loss_time < min_effective_green:
        cycle_length = min_effective_green + total_loss_time
    cycle_length = min(cycle_length, max_cycle)

    effective_green = cycle_length - total_loss_time

    downstream_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
    n_downstream = max(len(ego.downstream_queue), 1)
    avg_downstream = downstream_total / n_downstream
    spillback_penalty = max(0.0, avg_downstream - 5.0) * 2.0

    scores = {}
    for gp in green_phases:
        p = ego.phases.get(gp)
        if p is None:
            scores[gp] = 0.1
            continue

        local_pressure = p.queue + p.predicted_arrival
        hunger_bonus = p.waiting_time * 0.3
        upstream_bonus = ego.upstream_release_pressure * 0.2

        score = local_pressure + hunger_bonus + upstream_bonus - spillback_penalty
        scores[gp] = max(score, 0.1)

    total_score = sum(scores.values())

    green_times = {}
    for gp in green_phases:
        if total_score > 0:
            ratio = scores[gp] / total_score
        else:
            ratio = 1.0 / len(green_phases)

        gt = effective_green * ratio
        green_times[gp] = max(min_green, min(max_green, gt))

    final_cycle = sum(green_times.values()) + total_loss_time
    final_cycle = max(min_cycle, min(max_cycle, final_cycle))

    plan_result = CyclePlan(
        cycle_length=final_cycle,
        green_times=green_times,
        phase_order=green_phases
    )
    return plan_result