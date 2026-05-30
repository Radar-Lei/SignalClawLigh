#!/usr/bin/env python3
"""运行 baseline 对比实验：FixedTime, MaxPressure-CyclicAllocation, MaxPressure-Canonical,
MaxPressure-SwitchLossAware, SignalClaw-Seed

使用成都场景 (sumo_scenarios/chengdu/) 运行，统一随机种子 seed=42。
结果保存到 results/baseline_comparison.json。
"""

import os
import sys
import json
import time

# 确保项目根目录在 sys.path 中
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from signalclaw.skills.max_pressure import (
    MaxPressureCyclicAllocation,
    MaxPressureCanonical,
    MaxPressureSwitchLossAware,
)
from signalclaw.experiments.runner import ExperimentRunner


# ======================================================================
# 包装器：让 MaxPressure 变体兼容 ExperimentRunner 的 plan_cycle 接口
# ======================================================================

class MaxPressurePlanCycleAdapter:
    """将 MaxPressure 变体的 plan() 适配为 plan_cycle()。

    ExperimentRunner._control_with_legacy_skill 调用的是 skill.plan_cycle(obs)，
    但 MaxPressure 变体只提供 plan(obs) 方法。此适配器将 plan_cycle 委托给 plan。
    """

    def __init__(self, skill):
        self._skill = skill

    def plan_cycle(self, obs):
        return self._skill.plan(obs)

    def reset(self):
        self._skill.reset()


# ======================================================================
# 主函数
# ======================================================================

def main():
    sumocfg_path = os.path.join(PROJECT_DIR, "sumo_scenarios", "chengdu", "chengdu.sumocfg")
    seed = 42
    decision_interval = 5.0
    sim_duration = 3600.0

    print("=" * 70)
    print("Baseline 对比实验")
    print("=" * 70)
    print(f"SUMO config: {sumocfg_path}")
    print(f"Seed: {seed}")
    print(f"Simulation duration: {sim_duration}s")
    print(f"Decision interval: {decision_interval}s")
    print()

    # 构建方法字典
    methods = {}

    # 1. FixedTime — 不做任何控制
    methods["FixedTime"] = None

    # 2. MaxPressure-CyclicAllocation — 旧默认变体（循环固定顺序 + 比例分配）
    methods["MaxPressure-CyclicAllocation"] = MaxPressurePlanCycleAdapter(
        MaxPressureCyclicAllocation(decision_interval=decision_interval)
    )

    # 3. MaxPressure-Canonical — 新默认变体（每个决策间隔自由选择最高压力相位）
    methods["MaxPressure-Canonical"] = MaxPressurePlanCycleAdapter(
        MaxPressureCanonical(decision_interval=decision_interval)
    )

    # 4. MaxPressure-SwitchLossAware — 考虑切换损失的变体
    methods["MaxPressure-SwitchLossAware"] = MaxPressurePlanCycleAdapter(
        MaxPressureSwitchLossAware(decision_interval=decision_interval)
    )

    # 运行传统方法（FixedTime + 3 种 MaxPressure）
    runner = ExperimentRunner(
        sumocfg_path=sumocfg_path,
        seed=seed,
        decision_interval=decision_interval,
        sim_duration=sim_duration,
    )

    total_start = time.time()

    for name, controller in methods.items():
        t0 = time.time()
        print(f"\n{'=' * 60}")
        print(f"Running method: {name}")
        print(f"{'=' * 60}")
        try:
            runner.results[name] = runner._run_simulation(name, controller, verbose=True)
        except Exception as e:
            print(f"  [{name}] FAILED: {e}")
            import traceback
            traceback.print_exc()
        elapsed = time.time() - t0
        print(f"  [{name}] Elapsed: {elapsed:.1f}s")

    # 5. SignalClaw-Seed（如果 cohort 存在）
    seed_cohort_path = os.path.join(PROJECT_DIR, "artifacts", "skills", "cohorts", "seed_cohort.json")
    neighbor_graph_path = os.path.join(PROJECT_DIR, "artifacts", "topology", "one_hop_neighbors.json")

    if os.path.exists(seed_cohort_path):
        t0 = time.time()
        try:
            runner.run_signalclaw_cohort(
                cohort_path=seed_cohort_path,
                neighbor_graph_path=neighbor_graph_path,
                method_name="SignalClaw-Seed",
                verbose=True,
            )
        except Exception as e:
            print(f"  [SignalClaw-Seed] FAILED: {e}")
            import traceback
            traceback.print_exc()
        elapsed = time.time() - t0
        print(f"  [SignalClaw-Seed] Elapsed: {elapsed:.1f}s")
    else:
        print(f"\n[SignalClaw-Seed] Skipped: cohort file not found at {seed_cohort_path}")

    total_elapsed = time.time() - total_start
    print(f"\nTotal experiment time: {total_elapsed:.1f}s")

    # ------------------------------------------------------------------
    # 输出比较表格
    # ------------------------------------------------------------------

    method_names = list(runner.results.keys())
    summaries = {name: m.summary() for name, m in runner.results.items()}

    # 表格列：5 个关键指标
    table_keys = [
        ("Avg Queue", "avg_queue"),
        ("Avg Waiting Time (s)", "avg_waiting_time"),
        ("Completed Vehicles", "completed_vehicles"),
        ("Avg Travel Time (s)", "avg_travel_time"),
        ("Total Stops", "total_stops"),
    ]

    # 计算列宽
    col_w = max(15, max(len(n) for n in method_names) + 2)
    label_w = 25
    width = label_w + col_w * len(method_names)

    print(f"\n{'=' * width}")
    print(f"{'BASELINE COMPARISON':^{width}}")
    print(f"{'=' * width}")

    # 表头
    header = f"{'Metric':<{label_w}}"
    for name in method_names:
        header += f" {name:>{col_w}}"
    print(header)
    print(f"{'-' * width}")

    for label, key in table_keys:
        vals = []
        for name in method_names:
            v = summaries.get(name, {}).get(key, 0)
            vals.append(v)

        if all(v is None for v in vals):
            continue

        # 找最优（lower is better，除了 completed_vehicles 是 higher is better）
        higher_better = (key == "completed_vehicles")
        comparable = [v if v is not None else (float('-inf') if higher_better else float('inf')) for v in vals]
        best_idx = comparable.index(max(comparable) if higher_better else min(comparable))

        line = f"{label:<{label_w}}"
        for i, v in enumerate(vals):
            marker = " *" if i == best_idx else "  "
            if v is None:
                line += f"{'N/A':>{col_w - 2}}{marker}"
            elif isinstance(v, float):
                line += f"{v:>{col_w - 2}.1f}{marker}"
            else:
                line += f"{v:>{col_w - 2}}{marker}"
        print(line)

    print(f"{'-' * width}")
    print("* = best value (lower is better, except Completed Vehicles)")

    # ------------------------------------------------------------------
    # 保存结果
    # ------------------------------------------------------------------

    output_path = os.path.join(PROJECT_DIR, "results", "baseline_comparison.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 只保存关键指标
    output = {}
    for name in method_names:
        s = summaries.get(name, {})
        output[name] = {
            "method": name,
            "avg_queue": s.get("avg_queue"),
            "avg_waiting_time": s.get("avg_waiting_time"),
            "completed_vehicles": s.get("completed_vehicles"),
            "avg_travel_time": s.get("avg_travel_time"),
            "total_stops": s.get("total_stops"),
        }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nResults saved to {output_path}")

    # 保存 CSV 格式的步骤数据（用于后续绘图）
    for name, m in runner.results.items():
        csv_path = os.path.join(PROJECT_DIR, "results", f"{name}_baseline_steps.csv")
        if "ALL" in m.step_metrics:
            import csv
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["step", "sim_time", "queue_total", "waiting_time_avg",
                                 "throughput", "delay_total", "stops"])
                for sm in m.step_metrics["ALL"]:
                    writer.writerow([sm.step, sm.sim_time, sm.queue_total,
                                     sm.waiting_time_avg, sm.throughput,
                                     sm.delay_total, sm.stops])
            print(f"  Step data saved: {csv_path}")


if __name__ == "__main__":
    main()
