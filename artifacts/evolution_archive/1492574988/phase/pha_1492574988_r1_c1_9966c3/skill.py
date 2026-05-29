import math

_idx = 0
_rem = 0.0
_ph = 0


def _hp(plan):
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def decide(obs, plan):
    global _idx, _rem, _ph

    ego = obs.ego
    order = plan.phase_order
    gtimes = plan.green_times

    DT = 5.0
    MIN_G = 10.0
    MAX_G = 60.0
    MAX_EXT = 5.0

    if not order:
        return PhaseCommand(action="hold", next_phase_id=ego.current_phase_id, duration=DT, reason_code="no_phases")

    h = _hp(plan)
    if _ph != h:
        _ph = h
        _idx = 0
        first = order[0]
        _rem = gtimes.get(first, 15.0)
        return PhaseCommand(action="switch", next_phase_id=first, duration=_rem, reason_code="new_plan")

    cur = order[_idx]
    planned_g = gtimes.get(cur, 15.0)
    elapsed = max(0.0, planned_g - _rem)

    # 最小绿灯保护
    if elapsed < MIN_G:
        _rem = max(0, _rem - DT)
        return PhaseCommand(action="hold", next_phase_id=cur, duration=DT, reason_code="min_green")

    # 超过最大绿灯，强制切换
    if elapsed >= MAX_G:
        nxt_i = (_idx + 1) % len(order)
        nxt = order[nxt_i]
        _idx = nxt_i
        _rem = gtimes.get(nxt, 15.0)
        return PhaseCommand(action="switch", next_phase_id=nxt, duration=_rem, reason_code="max_green")

    # 相位自然结束
    if _rem <= 0:
        nxt_i = (_idx + 1) % len(order)
        nxt = order[nxt_i]
        _idx = nxt_i
        _rem = gtimes.get(nxt, 15.0)
        return PhaseCommand(action="switch", next_phase_id=nxt, duration=_rem, reason_code="phase_end")

    phobs = ego.phases.get(cur)

    if phobs is not None:
        q = phobs.queue
        arr = phobs.predicted_arrival
        wait = phobs.waiting_time
        sf = max(phobs.saturation_flow, 1.0)

        spill = ego.downstream_spillback_risk
        dsq = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0

        # 获取下一相位信息
        nxt_i = (_idx + 1) % len(order)
        nxt = order[nxt_i]
        nxt_phobs = ego.phases.get(nxt)
        nxt_demand = 0.0
        nxt_wait = 0.0
        if nxt_phobs is not None:
            nxt_demand = nxt_phobs.queue + 0.5 * nxt_phobs.predicted_arrival
            nxt_wait = nxt_phobs.waiting_time

        # 在计划结束窗口内考虑调整
        if _rem <= 10.0:
            # 与CyclePlan协调的需求评分
            demand = (q + 0.5 * arr) / sf * 60.0
            wait_urg = min(wait / 20.0, 3.0)
            score = demand + 0.5 * wait_urg

            # 高需求 + 下游通畅 -> 延长
            if score > 3.0 and dsq < 15 and spill < 0.5:
                ext = min(MAX_EXT, score * 0.3, MAX_G - elapsed)
                if ext > 1.0:
                    new_rem = _rem + ext
                    _rem = new_rem - DT
                    return PhaseCommand(action="extend", next_phase_id=cur, duration=new_rem, reason_code="ext_q" + str(int(q)))

            # 防饥饿：等待时间过长适当延长
            if wait > 40.0 and spill < 0.6:
                ext = min(3.0, MAX_G - elapsed)
                if ext > 1.0:
                    new_rem = _rem + ext
                    _rem = new_rem - DT
                    return PhaseCommand(action="extend", next_phase_id=cur, duration=new_rem, reason_code="ext_wait" + str(int(wait)))

            # 空队列提前结束
            if q < 1.0 and elapsed >= MIN_G:
                _idx = nxt_i
                _rem = gtimes.get(nxt, 15.0)
                return PhaseCommand(action="switch", next_phase_id=nxt, duration=_rem, reason_code="early_q" + str(int(q)))

            # 让路给高需求下一相位
            if nxt_demand > 10.0 and nxt_wait > 25.0 and q < 3.0:
                _idx = nxt_i
                _rem = gtimes.get(nxt, 15.0)
                return PhaseCommand(action="switch", next_phase_id=nxt, duration=_rem, reason_code="yield_high_demand")

        # 下游拥堵缩短（与CyclePlan协调：spillback时减小绿灯）
        if spill > 0.7 and elapsed >= MIN_G and _rem > 5.0:
            sh = min(5.0, _rem - 5.0)
            if sh > 1.0:
                _rem -= sh
                return PhaseCommand(action="shorten", next_phase_id=cur, duration=_rem, reason_code="spill_shorten")

    _rem = max(0, _rem - DT)
    return PhaseCommand(action="hold", next_phase_id=cur, duration=DT, reason_code="hold")
