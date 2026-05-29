"""Phase micro adjuster seed for intersection cluster_J18_cluster_1159519336_J5."""
from typing import Dict, Optional
from signalclaw.core.state import NetworkObservation, CyclePlan, PhaseCommand

_extend_threshold = 3.0
_max_extend = 5.0
_decision_interval = 5.0

_phase_index: int = 0
_phase_remaining: float = 0.0
_current_plan_hash: int = 0


def _plan_hash(plan: "CyclePlan") -> int:
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def decide(obs: "NetworkObservation", plan: "CyclePlan") -> "PhaseCommand":
    global _phase_index, _phase_remaining, _current_plan_hash

    ego = obs.ego
    phase_order = plan.phase_order
    if not phase_order:
        return PhaseCommand(
            action="hold", next_phase_id=ego.current_phase_id,
            duration=_decision_interval, reason_code="no_phases",
        )

    ph = _plan_hash(plan)
    if _current_plan_hash != ph:
        _phase_index = 0
        _current_plan_hash = ph
        first_phase = phase_order[0]
        _phase_remaining = plan.green_times.get(first_phase, 15.0)
        return PhaseCommand(
            action="switch", next_phase_id=first_phase,
            duration=plan.green_times.get(first_phase, 15.0),
            reason_code="new_plan",
        )

    remaining = _phase_remaining

    if remaining <= 0:
        next_idx = (_phase_index + 1) % len(phase_order)
        next_phase = phase_order[next_idx]
        _phase_index = next_idx
        _phase_remaining = plan.green_times.get(next_phase, 15.0)
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=_phase_remaining, reason_code="phase_end",
        )

    current_phase = phase_order[_phase_index]
    phase_obs = ego.phases.get(current_phase)

    if phase_obs is not None and remaining <= 10.0:
        current_queue = phase_obs.queue
        downstream_risk = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0
        if current_queue > _extend_threshold and downstream_risk < 10:
            extend = min(_max_extend, _decision_interval)
            _phase_remaining = remaining + extend - _decision_interval
            return PhaseCommand(
                action="extend", next_phase_id=current_phase,
                duration=_phase_remaining + _decision_interval,
                reason_code=f"extend_high_demand_q{current_queue:.0f}",
            )
        if current_queue < 1.0 and remaining > 5.0:
            _phase_remaining = 0
            next_idx = (_phase_index + 1) % len(phase_order)
            next_phase = phase_order[next_idx]
            _phase_index = next_idx
            _phase_remaining = plan.green_times.get(next_phase, 15.0)
            return PhaseCommand(
                action="switch", next_phase_id=next_phase,
                duration=_phase_remaining,
                reason_code=f"early_switch_empty_q{current_queue:.0f}",
            )

    _phase_remaining -= _decision_interval
    return PhaseCommand(
        action="hold", next_phase_id=current_phase,
        duration=_decision_interval, reason_code="continuing",
    )


def _reset():
    global _phase_index, _phase_remaining, _current_plan_hash
    _phase_index = 0
    _phase_remaining = 0.0
    _current_plan_hash = 0
