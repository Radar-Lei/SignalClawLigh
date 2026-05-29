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

def decide(obs, plan):
    ego = obs.ego
    phase_order = plan.phase_order
    
    min_green = 10.0
    max_green = 60.0
    max_extend = 5.0
    max_shorten = 5.0
    
    if not phase_order:
        return PhaseCommand(action="hold", next_phase_id=ego.current_phase_id, duration=2.0, reason_code="no_phases")
        
    cur_id = ego.current_phase_id
    
    # 如果当前相位不在 phase_order 中（例如计划更新，或刚刚启动），切换到 phase_order[0]
    if cur_id not in phase_order:
        first_phase = phase_order[0]
        gt = plan.green_times.get(first_phase, 15.0)
        if gt < min_green:
            gt = min_green
        return PhaseCommand(action="switch", next_phase_id=first_phase, duration=gt, reason_code="sync_plan")
        
    elapsed = ego.current_phase_elapsed
    planned_green = plan.green_times.get(cur_id, 15.0)
    
    # 保底最小绿灯时间
    if planned_green < min_green:
        planned_green = min_green
        
    remaining = planned_green - elapsed
    
    cur_idx = phase_order.index(cur_id)
    nxt_idx = (cur_idx + 1) % len(phase_order)
    nxt_id = phase_order[nxt_idx]
    
    # 如果当前相位时间已结束（或即将结束）
    if remaining <= 0.5:
        gt = plan.green_times.get(nxt_id, 15.0)
        if gt < min_green:
            gt = min_green
        return PhaseCommand(action="switch", next_phase_id=nxt_id, duration=gt, reason_code="phase_end")
        
    cur_p = _phase_pressure(ego, cur_id)
    nxt_p = _phase_pressure(ego, nxt_id)
    
    po = ego.phases.get(cur_id)
    cur_q = po.queue if po is not None else 0.0
    
    ds_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
    n_ds = max(len(ego.downstream_queue), 1)
    avg_ds = ds_total / n_ds
    dsr = max(0.0, avg_ds - 5.0) * 2.0 + ego.downstream_spillback_risk * 2.5
    
    # 微调区域：仅在最小绿灯满足时进行
    if elapsed >= min_green:
        
        # 1. EXTEND: 高需求 + 低溢出 + 当前大于下一个 + 接近结束
        if cur_p > 4.0 and dsr < 10.0 and cur_p > nxt_p * 1.2 and remaining <= 10.0:
            max_possible_ext = max(0.0, max_green - elapsed - remaining)
            ext = min(max_extend, max(1.0, cur_p * 0.25), max_possible_ext)
            if ext >= 1.0:
                new_duration = remaining + ext
                return PhaseCommand(action="extend", next_phase_id=cur_id, duration=new_duration, reason_code="extend_p%.0f" % cur_p)
                
        # 2. EARLY SWITCH: 当前空了 + 下一个很堵
        if cur_q < 1.0 and cur_p < 2.0 and nxt_p > 3.0 and remaining > 3.0:
            gt = plan.green_times.get(nxt_id, 15.0)
            if gt < min_green:
                gt = min_green
            return PhaseCommand(action="switch", next_phase_id=nxt_id, duration=gt, reason_code="early_switch")
            
        # 3. SHORTEN: 溢出风险极大 + 下一相位更紧急
        if dsr > 12.0 and cur_p < nxt_p and remaining > 5.0:
            sh = min(max_shorten, remaining - 3.0)
            if sh > 0:
                new_duration = remaining - sh
                return PhaseCommand(action="shorten", next_phase_id=cur_id, duration=new_duration, reason_code="shorten_spill_r%.0f" % dsr)
                
    # 默认：继续执行当前相位，2秒后再决策
    return PhaseCommand(action="hold", next_phase_id=cur_id, duration=min(remaining, 2.0), reason_code="continuing")