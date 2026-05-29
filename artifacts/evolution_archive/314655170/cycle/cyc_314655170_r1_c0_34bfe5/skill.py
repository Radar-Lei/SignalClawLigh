def _calculate_demand(phase_obs):
    if phase_obs is None:
        return 1.0
    
    queue = max(phase_obs.queue, 0.0)
    arrival = max(phase_obs.predicted_arrival, 0.0)
    waiting = max(phase_obs.waiting_time, 0.0)
    sat_flow = phase_obs.saturation_flow
    
    # 基础需求基于排队和预测到达
    demand = queue * 2.0 + arrival
    
    # 如果有饱和流率，结合排队和到达计算清空时间需求
    if sat_flow > 0:
        time_needed = (queue + arrival) / sat_flow * 3600.0
        demand = max(demand, time_needed)
        
    # 加入等待时间权重，防止相位饥饿
    demand += waiting * 0.2
    
    return max(demand, 0.1)

def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=40.0, green_times={}, phase_order=[])

    demands = {}
    total_queue = 0.0
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        demands[gp] = _calculate_demand(phase_obs)
        if phase_obs is not None:
            total_queue += phase_obs.queue

    total_demand = sum(demands.values())

    min_total_green = len(green_phases) * 10.0
    max_total_green = len(green_phases) * 60.0
    
    # 直接使用总需求作为初始目标总绿灯时间
    target_green_sum = total_demand
    
    # 考虑下游溢出风险，适当压缩总绿灯时间
    spillback_risk = getattr(ego, 'downstream_spillback_risk', 0.0)
    if spillback_risk > 0:
        target_green_sum = target_green_sum * (1.0 - 0.3 * min(spillback_risk, 1.0))
        
    # 考虑上游释放压力，适当增加总绿灯时间
    release_pressure = getattr(ego, 'upstream_release_pressure', 0.0)
    if release_pressure > 0:
        target_green_sum = target_green_sum * (1.0 + 0.3 * min(release_pressure, 1.0))
        
    # 确保目标时间在合法范围内
    target_green_sum = max(min_total_green, min(max_total_green, target_green_sum))

    green_times = {}
    for gp in green_phases:
        if total_demand > 0:
            gt = target_green_sum * (demands[gp] / total_demand)
        else:
            gt = target_green_sum / len(green_phases)
        green_times[gp] = max(10.0, min(60.0, gt))

    # 迭代修正截断导致的总时间误差
    for _ in range(5):
        current_sum = sum(green_times.values())
        diff = target_green_sum - current_sum
        if abs(diff) < 1.0:
            break
            
        adjustable = []
        for gp in green_phases:
            if diff > 0 and green_times[gp] < 60.0:
                adjustable.append(gp)
            elif diff < 0 and green_times[gp] > 10.0:
                adjustable.append(gp)
                
        if not adjustable:
            break
            
        adjust_per_phase = diff / len(adjustable)
        for gp in adjustable:
            green_times[gp] = max(10.0, min(60.0, green_times[gp] + adjust_per_phase))

    cycle_length = max(40.0, min(180.0, sum(green_times.values())))

    return CyclePlan(
        cycle_length=cycle_length,
        green_times=green_times,
        phase_order=green_phases
    )