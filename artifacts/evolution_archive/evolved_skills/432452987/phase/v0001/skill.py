def _phase_score(ego, phase_id, avg_downstream):
    """计算相位评分，与CyclePlan的评分逻辑协调"""
    p = ego.phases.get(phase_id)
    if p is None:
        return 0.1
    q = getattr(p, 'queue', 0)
    a = getattr(p, 'predicted_arrival', 0)
    w = getattr(p, 'waiting_time', 0)
    up = getattr(ego, 'upstream_release_pressure', 0.0)
    local_pressure = q + a
    hunger_bonus = w * 0.3
    upstream_bonus = up * 0.2
    spillback_penalty = max(0.0, avg_downstream - 5.0) * 2.0
    return max(local_pressure + hunger_bonus + upstream_bonus - spillback_penalty, 0.1)


def decide(obs, plan):
    ego = obs.ego
    phase_order = plan.phase_order

    MIN_GREEN = 10.0
    MAX_GREEN = 60.0
    MAX_EXTEND = 5.0
    TICK = 3.0

    # 空计划处理
    if not phase_order:
        return PhaseCommand(
            action="hold",
            next_phase_id=ego.current_phase_id,
            duration=TICK,
            reason_code="no_plan",
        )

    cur_ph = ego.current_phase_id

    # 当前相位不在计划中，切换到计划第一个相位
    if cur_ph not in phase_order:
        first = phase_order[0]
        gt = plan.green_times.get(first, 15.0)
        return PhaseCommand(
            action="switch",
            next_phase_id=first,
            duration=min(gt, MAX_GREEN),
            reason_code="align_plan",
        )

    # 获取时间信息
    time_elapsed = getattr(ego, "time_in_phase", None)

    # 下游状况
    ds_dict = getattr(ego, "downstream_queue", None) or {}
    ds_total = sum(ds_dict.values()) if ds_dict else 0
    ds_cnt = max(len(ds_dict), 1)
    ds_avg = ds_total / ds_cnt
    spill_risk = getattr(ego, "downstream_spillback_risk", 0.0)

    # 计算当前相位评分
    cur_score = _phase_score(ego, cur_ph, ds_avg)

    # 计算其他相位最大评分
    max_other_score = 0.0
    for pid in phase_order:
        if pid != cur_ph:
            s = _phase_score(ego, pid, ds_avg)
            if s > max_other_score:
                max_other_score = s

    # 当前相位需求指标
    p_obs = ego.phases.get(cur_ph)
    cur_q = getattr(p_obs, "queue", 0) if p_obs else 0
    cur_a = getattr(p_obs, "predicted_arrival", 0) if p_obs else 0
    cur_demand = cur_q + cur_a

    # 确定下一相位
    c_idx = phase_order.index(cur_ph)
    n_idx = (c_idx + 1) % len(phase_order)
    n_ph = phase_order[n_idx]
    n_gt = plan.green_times.get(n_ph, 15.0)

    # 如果无法获取时间信息，使用简化的基于需求决策
    if time_elapsed is None:
        if cur_demand < 1 and max_other_score > 8:
            return PhaseCommand(
                action="switch",
                next_phase_id=n_ph,
                duration=min(n_gt, MAX_GREEN),
                reason_code="demand_switch",
            )
        return PhaseCommand(
            action="hold",
            next_phase_id=cur_ph,
            duration=TICK,
            reason_code="no_time_hold",
        )

    planned_green = plan.green_times.get(cur_ph, 20.0)
    remain = planned_green - time_elapsed

    # === 最小绿灯保护 ===
    if time_elapsed < MIN_GREEN:
        dur = max(1.0, min(TICK, MIN_GREEN - time_elapsed))
        return PhaseCommand("hold", cur_ph, dur, "min_green")

    # === 最大绿灯限制 ===
    if time_elapsed >= MAX_GREEN:
        return PhaseCommand("switch", n_ph, min(n_gt, MAX_GREEN), "max_green")

    # === 下游溢出保护 ===
    if spill_risk > 0.6 and ds_total > 12:
        return PhaseCommand("shorten", n_ph, min(n_gt, MAX_GREEN), "spillback_protect")

    # === 计划时间结束，考虑延长 ===
    if remain <= 0:
        max_possible_ext = MAX_GREEN - time_elapsed
        can_extend = max_possible_ext > 1.0
        want_extend = cur_demand > 3 and cur_score > max_other_score * 1.5
        safe_extend = spill_risk < 0.4

        if can_extend and want_extend and safe_extend:
            ext = min(MAX_EXTEND, max_possible_ext)
            ext = max(1.0, min(ext, cur_demand * 0.5))
            return PhaseCommand("extend", cur_ph, ext, "extend_by_score")

        return PhaseCommand("switch", n_ph, min(n_gt, MAX_GREEN), "plan_end")

    # === 提前切换条件 ===
    if time_elapsed >= MIN_GREEN and remain > 2:
        # 当前相位需求极低且其他相位评分高
        if cur_demand < 2 and max_other_score > cur_score * 3:
            return PhaseCommand("shorten", n_ph, min(n_gt, MAX_GREEN), "early_low_demand")

        # 排队已清空且无预测到达
        if cur_q < 1 and cur_a < 1 and max_other_score > 5:
            return PhaseCommand("shorten", n_ph, min(n_gt, MAX_GREEN), "queue_cleared")

    # === 正常保持 ===
    dur = max(1.0, min(TICK, remain))
    return PhaseCommand("hold", cur_ph, dur, "hold")