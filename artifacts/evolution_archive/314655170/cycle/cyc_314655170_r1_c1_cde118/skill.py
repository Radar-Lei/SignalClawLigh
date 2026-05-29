def plan(obs):
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=40.0, green_times={}, phase_order=[])

    MIN_GREEN = 10.0
    MAX_GREEN = 60.0
    MIN_CYCLE = 40.0
    MAX_CYCLE = 180.0
    YELLOW_TIME = 3.0
    ALL_RED_TIME = 2.0

    num_phases = len(green_phases)
    total_loss_time = num_phases * (YELLOW_TIME + ALL_RED_TIME)

    scores = {}
    total_queue = 0.0

    for gp in green_phases:
        phase = ego.phases.get(gp)
        if phase is None:
            scores[gp] = 0.1
            continue

        q = phase.queue
        wt = phase.waiting_time
        pa = phase.predicted_arrival

        total_queue += q

        # 综合评分：排队、等待、预测到达
        score = q * 1.0 + wt * 0.5 + pa * 0.8

        # 考虑下游溢出风险和上游释放压力
        score = score - ego.downstream_spillback_risk * q * 0.5
        score = score + ego.upstream_release_pressure * 2.0

        scores[gp] = max(0.1, score)

    # 确定满足物理意义的最小周期和目标周期
    min_total_green = num_phases * MIN_GREEN
    min_valid_cycle = min_total_green + total_loss_time

    target_cycle = min(MAX_CYCLE, max(MIN_CYCLE, min_valid_cycle))

    # 根据总排队长度动态拉伸周期
    if total_queue > 30:
        target_cycle = min(MAX_CYCLE, max(target_cycle, target_cycle + (total_queue - 30) * 1.5))

    # 计算可以分配的总绿灯时间
    target_total_green = target_cycle - total_loss_time
    target_total_green = max(min_total_green, target_total_green)
    target_cycle = target_total_green + total_loss_time

    sum_scores = sum(scores.values())
    if sum_scores == 0:
        sum_scores = 1.0

    # 初始按比例分配
    green_times = {}
    for gp in green_phases:
        green_times[gp] = target_total_green * (scores[gp] / sum_scores)

    # 迭代调整以满足 MIN_GREEN 和 MAX_GREEN 约束
    for _ in range(num_phases + 2):
        fixed_green_sum = 0.0
        unfixed_score_sum = 0.0
        unfixed_phases_count = 0

        for gp in green_phases:
            gt = green_times[gp]
            if gt <= MIN_GREEN:
                fixed_green_sum += MIN_GREEN
            elif gt >= MAX_GREEN:
                fixed_green_sum += MAX_GREEN
            else:
                unfixed_score_sum += scores[gp]
                unfixed_phases_count += 1

        remaining_green = target_total_green - fixed_green_sum

        all_fixed = True
        for gp in green_phases:
            gt = green_times[gp]
            if gt <= MIN_GREEN:
                green_times[gp] = MIN_GREEN
            elif gt >= MAX_GREEN:
                green_times[gp] = MAX_GREEN
            else:
                all_fixed = False
                if unfixed_score_sum > 0:
                    green_times[gp] = remaining_green * (scores[gp] / unfixed_score_sum)
                else:
                    green_times[gp] = max(MIN_GREEN, remaining_green / unfixed_phases_count if unfixed_phases_count > 0 else 0)

        if all_fixed:
            break

    final_green_times = {}
    for gp in green_phases:
        final_green_times[gp] = max(MIN_GREEN, min(MAX_GREEN, green_times[gp]))

    final_cycle = sum(final_green_times.values()) + total_loss_time
    final_cycle = max(MIN_CYCLE, min(MAX_CYCLE, final_cycle))

    return CyclePlan(
        cycle_length=final_cycle,
        green_times=final_green_times,
        phase_order=green_phases,
        offset_target=0.0
    )