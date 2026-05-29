"""Improved Phase micro adjuster for intersection cluster_J100_J94_J95_J99."""

_decision_interval = 5.0
_max_extend = 5.0
_max_shorten = 5.0
_min_green = 10.0
_max_green = 60.0

_phase_index = 0
_phase_remaining = 0.0
_current_plan_hash = 0
_elapsed = 0.0


def _plan_hash(plan):
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def _phase_pressure(ego, phase_id):
    """Pressure score aligned with CyclePlan scoring for consistency."""
    po = ego.phases.get(phase_id)
    if po is None:
        return 0.1
    local = po.queue * 1.2 + po.predicted_arrival * 0.8
    hunger = po.waiting_time * 0.5
    ds_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
    n_ds = max(len(ego.downstream_queue), 1)
    avg_ds = ds_total / n_ds
    spill = max(0.0, avg_ds - 5.0) * 2.0 + ego.downstream_spillback_risk * 2.5
    up = ego.upstream_release_pressure * 1.0
    return max(local + hunger - spill + up, 0.1)


def _ds_risk(ego):
    ds_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
    n_ds = max(len(ego.downstream_queue), 1)
    avg_ds = ds_total / n_ds
    return max(0.0, avg_ds - 5.0) * 2.0 + ego.downstream_spillback_risk * 2.5


def decide(obs, plan):
    global _phase_index, _phase_remaining, _current_plan_hash, _elapsed

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
        _elapsed = 0.0
        fp = phase_order[0]
        gt = plan.green_times.get(fp, 15.0)
        _phase_remaining = gt
        return PhaseCommand(
            action="switch", next_phase_id=fp,
            duration=gt, reason_code="new_plan",
        )

    remaining = _phase_remaining

    if remaining <= 0:
        ni = (_phase_index + 1) % len(phase_order)
        nxt_ph = phase_order[ni]
        _phase_index = ni
        _elapsed = 0.0
        gt = plan.green_times.get(nxt_ph, 15.0)
        _phase_remaining = gt
        return PhaseCommand(
            action="switch", next_phase_id=nxt_ph,
            duration=gt, reason_code="phase_end",
        )

    cur_id = phase_order[_phase_index]
    ni = (_phase_index + 1) % len(phase_order)
    nxt_id = phase_order[ni]

    cur_p = _phase_pressure(ego, cur_id)
    nxt_p = _phase_pressure(ego, nxt_id)
    dsr = _ds_risk(ego)

    po = ego.phases.get(cur_id)
    cur_q = po.queue if po is not None else 0.0

    el = _elapsed

    # Micro-adjustment only after min_green substantially met
    if el >= _min_green - _decision_interval:

        # EXTEND: high demand + low spillback + current >> next + near end of green
        if cur_p > 4.0 and dsr < 10.0 and cur_p > nxt_p * 1.3 and remaining <= 15.0:
            max_ext = max(0.0, _max_green - el - remaining)
            ext = min(_max_extend, max(1.0, cur_p * 0.25), max_ext)
            if ext >= 1.0:
                nr = remaining + ext - _decision_interval
                if nr > 0:
                    _phase_remaining = nr
                    _elapsed += _decision_interval
                    return PhaseCommand(
                        action="extend", next_phase_id=cur_id,
                        duration=nr + _decision_interval,
                        reason_code="extend_p%.0f" % cur_p,
                    )

        # EARLY SWITCH: current empty + next hungry
        if cur_q < 1.0 and cur_p < 2.0 and nxt_p > 3.0 and remaining > 3.0:
            _phase_index = ni
            _elapsed = 0.0
            gt = plan.green_times.get(nxt_id, 15.0)
            _phase_remaining = gt
            return PhaseCommand(
                action="switch", next_phase_id=nxt_id,
                duration=gt,
                reason_code="early_switch_q%.0f" % cur_q,
            )

        # SHORTEN: downstream congested + next phase more urgent
        if dsr > 12.0 and cur_p < nxt_p and remaining > 5.0:
            sh = min(_max_shorten, remaining - 3.0)
            if sh > 0:
                nr = remaining - sh - _decision_interval
                if nr > 0:
                    _phase_remaining = nr
                    _elapsed += _decision_interval
                    return PhaseCommand(
                        action="shorten", next_phase_id=cur_id,
                        duration=nr + _decision_interval,
                        reason_code="shorten_spill_r%.0f" % dsr,
                    )

    # Default: hold with plan
    _phase_remaining -= _decision_interval
    _elapsed += _decision_interval
    return PhaseCommand(
        action="hold", next_phase_id=cur_id,
        duration=_decision_interval, reason_code="continuing",
    )


def _reset():
    global _phase_index, _phase_remaining, _current_plan_hash, _elapsed
    _phase_index = 0
    _phase_remaining = 0.0
    _current_plan_hash = 0
    _elapsed = 0.0