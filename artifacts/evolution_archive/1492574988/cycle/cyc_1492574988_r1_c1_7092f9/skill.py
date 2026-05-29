import math

def plan(obs):
    """改进的交通信号控制算法 - 多维度压力感知和溢出预防"""
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    num_phases = len(green_phases)
    
    # 约束参数
    MIN_GREEN = 10.0
    MAX_GREEN = 60.0
    MIN_CYCLE = 40.0
    MAX_CYCLE = 180.0
    YELLOW_TIME = 3.0
    ALL_RED_TIME = 2.0
    
    if num_phases == 0:
        return CyclePlan(cycle_length=80.0, green_times={}, phase_order=[])
    
    # 每个周期的损失时间（黄灯+全红）
    total_lost_time = num_phases * (YELLOW_TIME + ALL_RED_TIME)
    
    # === 计算各相位的压力评分 ===
    scores = {}
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            scores[gp] = 0.1
            continue
        
        # 排队压力 - 直接反映当前需求
        queue_score = phase_obs.queue * 1.5
        
        # 等待时间压力 - 使用对数平滑，避免极端值主导
        wait_score = math.log1p(phase_obs.waiting_time) * 2.5
        
        # 到达预测 - 反映未来需求
        arrival_score = phase_obs.predicted_arrival * 0.5
        
        # 上游释放压力
        upstream_score = ego.upstream_release_pressure * 0.4
        
        # 下游溢出风险 - 分级惩罚
        spillback_risk = ego.downstream_spillback_risk
        spillback_penalty = spillback_risk * (5.0 if spillback_risk > 0.5 else 2.0)
        
        # 饱和流率因子 - 高流率需要更多绿灯时间
        sat_factor = max(0.5, min(1.5, phase_obs.saturation_flow / 1800.0))
        
        # 综合评分
        raw_score = queue_score + wait_score + arrival_score + upstream_score - spillback_penalty
        scores[gp] = max(raw_score * sat_factor, 0.1)
    
    # === 计算周期长度 ===
    total_queue = sum(p.queue for p in ego.phases.values())
    total_arrival = sum(p.predicted_arrival for p in ego.phases.values())
    avg_waiting = sum(p.waiting_time for p in ego.phases.values()) / num_phases
    
    # 基于排队的周期
    if total_queue <= 5:
        base_cycle = 50.0
    elif total_queue >= 50:
        base_cycle = 130.0
    else:
        base_cycle = 50.0 + (total_queue - 5.0) * 80.0 / 45.0
    
    # 基于到达预测的调整
    arrival_adj = 15.0 if total_arrival > 30 else (8.0 if total_arrival > 15 else 0.0)
    
    # 基于平均等待时间的调整
    wait_adj = 15.0 if avg_waiting > 60 else (8.0 if avg_waiting > 30 else 0.0)
    
    # 综合周期
    target_cycle = base_cycle + arrival_adj + wait_adj
    
    # 确保能容纳所有相位的最小绿灯
    min_required = num_phases * MIN_GREEN + total_lost_time
    target_cycle = max(target_cycle, min_required)
    
    cycle_length = max(MIN_CYCLE, min(MAX_CYCLE, target_cycle))
    
    # === 分配绿灯时间 ===
    available_green = max(cycle_length - total_lost_time, num_phases * MIN_GREEN)
    
    total_score = sum(scores.values())
    green_times = {}
    
    if total_score > 0:
        for gp in green_phases:
            ratio = scores[gp] / total_score
            raw_gt = available_green * ratio
            green_times[gp] = max(MIN_GREEN, min(MAX_GREEN, raw_gt))
    else:
        equal_gt = available_green / num_phases
        for gp in green_phases:
            green_times[gp] = max(MIN_GREEN, min(MAX_GREEN, equal_gt))
    
    # === 调整绿灯时间以满足约束 ===
    actual_green_sum = sum(green_times.values())
    max_allowed = MAX_CYCLE - total_lost_time
    
    if actual_green_sum > max_allowed:
        scale = max_allowed / actual_green_sum
        for gp in green_phases:
            green_times[gp] = max(MIN_GREEN, green_times[gp] * scale)
        actual_green_sum = sum(green_times.values())
    
    # 最终周期
    final_cycle = actual_green_sum + total_lost_time
    final_cycle = max(MIN_CYCLE, min(MAX_CYCLE, final_cycle))
    
    return CyclePlan(
        cycle_length=final_cycle,
        green_times=green_times,
        phase_order=green_phases,
    )