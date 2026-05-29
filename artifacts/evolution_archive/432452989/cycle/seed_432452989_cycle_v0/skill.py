"""Cycle planner seed for intersection 432452989."""
from collections import deque
from typing import Dict, List, Optional
from signalclaw.core.state import NetworkObservation, CyclePlan

_min_green = 10.0
_max_green = 60.0
_base_cycle = 80.0
_history_window = 5

_pressure_history: Dict[int, deque] = {}
_last_green_time: Dict[int, float] = {}


def _get_history(phase_id: int) -> deque:
    if phase_id not in _pressure_history:
        _pressure_history[phase_id] = deque(maxlen=_history_window)
    return _pressure_history[phase_id]


def plan(obs: "NetworkObservation") -> "CyclePlan":
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=_base_cycle, green_times={}, phase_order=[])

    scores = {}
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            scores[gp] = 0.0
            continue
        local_pressure = phase_obs.queue
        downstream_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
        n_downstream = max(len(ego.downstream_queue), 1)
        avg_downstream = downstream_total / n_downstream
        spillback_risk = max(0, avg_downstream - 5.0) * 2.0
        arrival_prediction = phase_obs.waiting_time * 0.3
        last_served = _last_green_time.get(gp, 0)
        hunger_time = obs.timestamp - last_served
        hunger_bonus = min(hunger_time * 0.5, 15.0)
        score = (
            1.0 * local_pressure
            + 0.4 * arrival_prediction
            - 0.8 * spillback_risk
            + 0.6 * hunger_bonus
        )
        scores[gp] = score
        _get_history(gp).append(score)

    total_queue = sum(p.queue for p in ego.phases.values())
    if total_queue < 5:
        cycle_length = _base_cycle * 0.7
    elif total_queue > 50:
        cycle_length = _base_cycle * 1.3
    else:
        cycle_length = _base_cycle * (0.7 + 0.6 * (total_queue - 5) / 45.0)
    cycle_length = max(40.0, min(180.0, cycle_length))

    min_score = min(scores.values())
    shifted = {gp: max(s - min_score + 1.0, 0.1) for gp, s in scores.items()}

    for gp in green_phases:
        hist = _get_history(gp)
        if len(hist) > 1:
            avg_hist = sum(hist) / len(hist)
            shifted[gp] = 0.7 * shifted[gp] + 0.3 * max(avg_hist - min_score + 1.0, 0.1)

    total_score = sum(shifted.values())
    green_times = {}
    for gp in green_phases:
        if total_score > 0:
            gt = cycle_length * (shifted[gp] / total_score)
        else:
            gt = cycle_length / len(green_phases)
        green_times[gp] = max(_min_green, min(_max_green, gt))

    plan = CyclePlan(
        cycle_length=sum(green_times.values()),
        green_times=green_times,
        phase_order=green_phases,
    )
    return plan


def _reset():
    _pressure_history.clear()
    _last_green_time.clear()
