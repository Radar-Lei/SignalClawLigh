import math

def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=80.0, green_times={}, phase_order=[])
    
    # 参数配置
    min_green = 10.0
    max_green = 60.0
    min_cycle = 40.0
    max_cycle = 180.0
    base_cycle = 80.0
    yellow_time = 3.0
    all_red_time = 2.0
    
    # 计算每个相位的压力值
    phase_pressures = {}
    total_queue = 0.0
    total_waiting_time = 0.0
    
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            phase_pressures[gp] = 0.0
            continue
        
        queue = phase_obs.queue
        waiting_time = phase_obs.waiting_time
        predicted_arrival = phase_obs.predicted_arrival
        saturation_flow = phase_obs.saturation_flow
        
        total_queue += queue
        total_waiting_time += waiting_time
        
        # 计算基础压力值（排队车辆 + 等待时间 + 预测到达车辆）
        pressure = queue * 1.0 + waiting_time * 0.2 + predicted_arrival * 0.5
        
        # 考虑饱和流率调整（高饱和流率说明通行能力强，可适当降低压力）
        if saturation_flow > 0:
            pressure = pressure * (1.0 / (1.0 + saturation_flow * 0.01))
        
        phase_pressures[gp] = pressure
    
    # 根据总排队长度动态调整周期长度
    if total_queue < 5:
        cycle_length = base_cycle * 0.7
    elif total_queue > 50:
        cycle_length = base_cycle * 1.3
    else:
        cycle_length = base_cycle * (0.7 + 0.6 * (total_queue - 5) / 45.0)
    
    # 考虑平均等待时间进一步调整周期
    if len(green_phases) > 0:
        avg_waiting = total_waiting_time / len(green_phases)
        if avg_waiting > 60:
            cycle_length *= 1.2
        elif avg_waiting > 30:
            cycle_length *= 1.1
    
    # 考虑下游溢出风险缩短周期
    if hasattr(ego, 'downstream_spillback_risk') and ego.downstream_spillback_risk > 0.5:
        cycle_length *= max(0.8, 1.0 - ego.downstream_spillback_risk * 0.2)
    
    # 考虑上游释放压力延长周期
    if hasattr(ego, 'upstream_release_pressure') and ego.upstream_release_pressure > 0.5:
        cycle_length *= min(1.2, 1.0 + ego.upstream_release_pressure * 0.1)
    
    # 约束周期长度
    cycle_length = max(min_cycle, min(max_cycle, cycle_length))
    
    # 减去黄灯和全红时间（假设每个相位之间有黄灯+全红）
    total_lost_time = (yellow_time + all_red_time) * len(green_phases)
    available_green_time = max(min_cycle, cycle_length - total_lost_time)
    
    # 调整压力值，确保所有相位都能获得最小绿灯时间
    total_pressure = sum(phase_pressures.values())
    if total_pressure <= 0:
        # 如果没有压力，均匀分配
        green_times = {gp: available_green_time / len(green_phases) for gp in green_phases}
    else:
        # 按压力比例分配绿灯时间
        min_pressure = min(phase_pressures.values())
        shifted_pressures = {gp: max(p - min_pressure + 1.0, 0.1) for gp, p in phase_pressures.items()}
        total_shifted = sum(shifted_pressures.values())
        
        green_times = {}
        for gp in green_phases:
            allocated = available_green_time * (shifted_pressures[gp] / total_shifted)
            green_times[gp] = allocated
    
    # 应用绿灯时间约束
    for gp in green_phases:
        green_times[gp] = max(min_green, min(max_green, green_times[gp]))
    
    # 重新计算实际周期长度（绿灯时间总和 + 损失时间）
    actual_cycle_length = sum(green_times.values()) + total_lost_time
    actual_cycle_length = max(min_cycle, min(max_cycle, actual_cycle_length))
    
    # 如果实际周期超出约束，按比例调整绿灯时间
    if actual_cycle_length != sum(green_times.values()) + total_lost_time:
        scale_factor = (actual_cycle_length - total_lost_time) / sum(green_times.values())
        green_times = {gp: max(min_green, min(max_green, green_times[gp] * scale_factor)) for gp in green_phases}
        actual_cycle_length = sum(green_times.values()) + total_lost_time
    
    return CyclePlan(
        cycle_length=actual_cycle_length,
        green_times=green_times,
        phase_order=green_phases,
        offset_target=None
    )