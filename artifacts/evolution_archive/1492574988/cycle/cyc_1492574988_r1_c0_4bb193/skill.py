import math

_min_green = 10.0
_max_green = 60.0
_min_cycle = 40.0
_max_cycle = 180.0

_last_green_time = {}


def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    num_phases = len(green_phases)

    if not green_phases:
        return CyclePlan(cycle_length=80.0, green_times={}, phase_order=[])

    # === Compute phase demand scores ===
    scores = {}
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            scores[gp] = 0.5
            continue

        queue = phase_obs.queue
        arrivals = phase_obs.predicted_arrival
        waiting = phase_obs.waiting_time
        sat_flow = max(phase_obs.saturation_flow, 1.0)

        # Hunger factor - how long since phase was last served
        last_served = _last_green_time.get(gp, 0)
        hunger = obs.timestamp - last_served
        hunger_factor = min(hunger / 45.0, 2.0)

        # Saturation-degree-based green time need
        total_demand = queue + 0.5 * arrivals
        green_need = total_demand / sat_flow * 60.0

        # Waiting urgency - non-linear to prevent starvation
        wait_urgency = min(waiting / 20.0, 3.0)

        score = green_need + 2.0 * wait_urgency + 1.5 * hunger_factor
        scores[gp] = max(score, 0.1)

    # === Downstream spillback adjustment ===
    spillback = ego.downstream_spillback_risk
    if spillback > 0.3:
        factor = max(0.4, 1.0 - 0.35 * min(spillback, 1.5))
        for gp in green_phases:
            scores[gp] *= factor

    # === Upstream release pressure adjustment ===
    upstream = ego.upstream_release_pressure
    if upstream > 0.3:
        for gp in green_phases:
            phase_obs = ego.phases.get(gp)
            if phase_obs is not None and phase_obs.queue > 3:
                scores[gp] *= 1.0 + 0.2 * min(upstream, 2.0)

    # === Cycle length calculation ===
    phase_values = [p for p in ego.phases.values() if p is not None]
    total_queue = sum(p.queue for p in phase_values)
    avg_wait = sum(p.waiting_time for p in phase_values) / max(num_phases, 1)

    # Webster-inspired cycle length
    if total_queue < 5:
        base_cycle = 55.0
    elif total_queue > 55:
        base_cycle = 145.0
    else:
        base_cycle = 55.0 + 90.0 * (total_queue - 5) / 50.0

    # Adjust for average waiting time
    base_cycle += min(avg_wait * 0.1, 12.0)

    # Shorter cycles during spillback to prevent overflow
    if spillback > 0.5:
        base_cycle *= max(0.8, 1.0 - 0.1 * spillback)

    target_cycle = max(_min_cycle, min(_max_cycle, base_cycle))

    # === Distribute green time proportionally to scores ===
    total_score = sum(scores.values())
    green_times = {}
    for gp in green_phases:
        if total_score > 0:
            ratio = scores[gp] / total_score
        else:
            ratio = 1.0 / num_phases
        gt = target_cycle * ratio
        green_times[gp] = max(_min_green, min(_max_green, gt))

    # === Iterative redistribution to match target cycle ===
    for _ in range(5):
        total_green = sum(green_times.values())
        diff = total_green - target_cycle

        if abs(diff) < 0.5:
            break

        if diff > 0:
            # Need to reduce: take from phases above min_green
            adjustable = {}
            for gp in green_phases:
                room = green_times[gp] - _min_green
                if room > 0.1:
                    adjustable[gp] = room
        else:
            # Need to increase: add to phases below max_green
            adjustable = {}
            for gp in green_phases:
                room = _max_green - green_times[gp]
                if room > 0.1:
                    adjustable[gp] = room

        if not adjustable:
            break

        adj_total = sum(adjustable.values())
        if adj_total <= 0:
            break

        for gp in adjustable:
            proportion = adjustable[gp] / adj_total
            adjustment = diff * proportion
            green_times[gp] = max(_min_green, min(_max_green, green_times[gp] - adjustment))

    actual_cycle = sum(green_times.values())

    # Update last green tracking
    for gp in green_phases:
        _last_green_time[gp] = obs.timestamp

    return CyclePlan(
        cycle_length=actual_cycle,
        green_times=green_times,
        phase_order=green_phases,
    )