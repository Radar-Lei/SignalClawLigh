def _plan_hash(plan):
    """计算计划哈希值用于检测变化"""
    items = tuple((k, round(v, 2)) for k, v in sorted(plan.green_times.items()))
    return hash((round(plan.cycle_length, 2), items, tuple(plan.phase_order)))


def _calc_demand(phase_obs):
    """计算相位综合需求分数，权重与CyclePlan一致: queue*2 + arrival*1 + waiting*0.5"""
    if phase_obs is None:
        return 0.0
    q = max(getattr(phase_obs, 'queue', 0.0), 0.0)
    a = max(getattr(phase_obs, 'predicted_arrival', 0.0), 0.0)
    w = max(getattr(phase_obs, 'waiting_time', 0.0), 0.0)
    return q * 2.0 + a * 1.0 + w * 0.5


def decide(obs, plan):
    """改进的PhaseMicroSkill - 与CyclePlan协调的动态相位微调"""

    # 使用函数属性存储跟踪状态（非全局变量）
    if not hasattr(decide, '_phase_idx'):
        decide._phase_idx = 0
        decide._remaining = 0.0
        decide._p_hash = 0

    ego = obs.ego
    phase_order = plan.phase_order

    # 参数
    INTERVAL = 3.0
    MIN_GREEN = 10.0
    MAX_GREEN = 60.0
    MAX_EXTEND = 5.0
    MAX_SHORTEN = 5.0

    # 边界：无相位可用
    if not phase_order:
        return PhaseCommand(
            action="hold",
            next_phase_id=getattr(ego, 'current_phase_id', 0),
            duration=INTERVAL,
            reason_code="no_phases"
        )

    # 检测计划更新
    p_hash = _plan_hash(plan)
    if decide._p_hash != p_hash:
        decide._p_hash = p_hash
        decide._phase_idx = 0
        fp = phase_order[0]
        fg = plan.green_times.get(fp, 15.0)
        decide._remaining = fg
        return PhaseCommand(
            action="switch",
            next_phase_id=fp,
            duration=fg,
            reason_code="new_plan"
        )

    rem = decide._remaining

    # 当前相位时间用完，轮转到下一个
    if rem <= 0:
        ni = (decide._phase_idx + 1) % len(phase_order)
        nph = phase_order[ni]
        decide._phase_idx = ni
        ng = plan.green_times.get(nph, 15.0)
        decide._remaining = ng
        return PhaseCommand(
            action="switch",
            next_phase_id=nph,
            duration=ng,
            reason_code="phase_end"
        )

    # 当前相位信息
    cur_phase = phase_order[decide._phase_idx]
    cur_obs = ego.phases.get(cur_phase)
    planned_g = plan.green_times.get(cur_phase, 15.0)
    elapsed = planned_g - rem

    # 当前与下一相位需求
    cur_d = _calc_demand(cur_obs)
    ni = (decide._phase_idx + 1) % len(phase_order)
    nph = phase_order[ni]
    nxt_d = _calc_demand(ego.phases.get(nph))

    # 下游状态评估
    ds_q = sum(ego.downstream_queue.values()) if getattr(ego, 'downstream_queue', None) else 0.0
    ds_r = float(ego.downstream_spillback_risk) if getattr(ego, 'downstream_spillback_risk', None) else 0.0
    up_p = float(ego.upstream_release_pressure) if getattr(ego, 'upstream_release_pressure', None) else 0.0

    # 仅在满足最小绿灯后允许动态调整
    if elapsed >= MIN_GREEN:

        # === 延长：高需求 + 下游安全 + 接近尾声 ===
        if rem <= 12.0 and cur_d > 5.0 and ds_r < 5.0 and ds_q < 15.0:
            ext = min(MAX_EXTEND, max(1.0, cur_d * 0.2))
            if elapsed + rem + ext <= MAX_GREEN:
                decide._remaining = rem + ext
                return PhaseCommand(
                    action="extend",
                    next_phase_id=cur_phase,
                    duration=decide._remaining,
                    reason_code="extend_demand"
                )

        # === 缩短：下游溢出风险高 ===
        if ds_r > 7.0 and rem > 5.0:
            sh = min(MAX_SHORTEN, rem - 3.0)
            if sh > 0:
                decide._remaining = max(1.0, rem - sh)
                return PhaseCommand(
                    action="shorten",
                    next_phase_id=cur_phase,
                    duration=decide._remaining,
                    reason_code="shorten_spillback"
                )

        # === 提前切换：当前几乎空 + 下一相位紧迫 ===
        if cur_d < 2.0 and nxt_d > 6.0 and rem > 3.0:
            decide._phase_idx = ni
            ng = plan.green_times.get(nph, 15.0)
            decide._remaining = ng
            return PhaseCommand(
                action="switch",
                next_phase_id=nph,
                duration=ng,
                reason_code="early_switch_next_urgent"
            )

        # === 缩短：低需求 + 无上游压力 ===
        if cur_d < 2.0 and up_p < 2.0 and rem > 6.0:
            sh = min(MAX_SHORTEN * 0.6, rem - 4.0)
            if sh > 1.0:
                decide._remaining = rem - sh
                return PhaseCommand(
                    action="shorten",
                    next_phase_id=cur_phase,
                    duration=decide._remaining,
                    reason_code="shorten_low_demand"
                )

    # === 正常保持 ===
    decide._remaining = rem - INTERVAL
    return PhaseCommand(
        action="hold",
        next_phase_id=cur_phase,
        duration=INTERVAL,
        reason_code="continuing"
    )