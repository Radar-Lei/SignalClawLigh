#!/usr/bin/env python3
"""
对比实验：Legacy SignalClaw vs SignalClaw-Seed

验证两条执行路径（legacy skill 直接控制 vs OnlineController 双 Skill 闭环）
在相同 SUMO 场景下的性能差异。
"""

import os
import sys
import json
import time

# 确保项目在 path 上
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signalclaw.experiments.runner import ExperimentRunner
from signalclaw.skills.signalclaw_skill import SignalClawSkill
from signalclaw.skills.max_pressure import MaxPressureSkill


def main():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    sumocfg_path = os.path.join(project_dir, "sumo_scenarios", "chengdu", "chengdu.sumocfg")
    seed_cohort_path = os.path.join(project_dir, "artifacts", "skills", "cohorts", "seed_cohort.json")
    neighbor_graph_path = os.path.join(project_dir, "artifacts", "topology", "one_hop_neighbors.json")
    output_dir = os.path.join(project_dir, "results_comparison")

    print(f"SUMO config: {sumocfg_path}")
    print(f"Seed cohort: {seed_cohort_path}")
    print(f"Neighbor graph: {neighbor_graph_path}")
    print(f"Output: {output_dir}")
    print()

    # --- 实验 1: 只跑 Legacy SignalClaw ---
    print("=" * 70)
    print("PHASE 1: Running Legacy SignalClaw")
    print("=" * 70)
    t0 = time.time()

    runner_legacy = ExperimentRunner(
        sumocfg_path=sumocfg_path,
        seed=42,
        decision_interval=5.0,
        sim_duration=3600.0,
    )
    # 只跑 SignalClaw legacy（不跑 FixedTime/MaxPressure）
    legacy_result = runner_legacy._run_simulation(
        "SignalClaw-Legacy",
        SignalClawSkill(decision_interval=5.0),
        verbose=True,
    )
    t1 = time.time()
    print(f"  Legacy SignalClaw completed in {t1 - t0:.1f}s")
    print()

    # --- 实验 2: 跑 SignalClaw-Seed ---
    print("=" * 70)
    print("PHASE 2: Running SignalClaw-Seed (OnlineController)")
    print("=" * 70)
    t2 = time.time()

    runner_seed = ExperimentRunner(
        sumocfg_path=sumocfg_path,
        seed=42,
        decision_interval=5.0,
        sim_duration=3600.0,
    )
    seed_result = runner_seed.run_signalclaw_cohort(
        cohort_path=seed_cohort_path,
        neighbor_graph_path=neighbor_graph_path,
        method_name="SignalClaw-Seed",
        verbose=True,
    )
    t3 = time.time()
    print(f"  SignalClaw-Seed completed in {t3 - t2:.1f}s")
    print()

    # --- 汇总对比 ---
    print("=" * 70)
    print("COMPARISON RESULTS")
    print("=" * 70)

    legacy_summary = legacy_result.summary()
    seed_summary = seed_result.summary()

    metrics = [
        ("avg_queue", "Avg Queue (time-weighted)", False),
        ("max_queue", "Max Queue", False),
        ("avg_travel_time", "Avg Travel Time (s)", False),
        ("avg_waiting_time", "Avg Waiting Time (s)", False),
        ("completed_vehicles", "Completed Vehicles", True),
        ("total_stops", "Total Stops", False),
    ]

    print(f"{'Metric':<30} {'Legacy':>15} {'Seed':>15} {'Delta%':>10}")
    print("-" * 75)

    for key, label, higher_better in metrics:
        lv = legacy_summary.get(key)
        sv = seed_summary.get(key)
        if lv is None or sv is None:
            print(f"{label:<30} {str(lv):>15} {str(sv):>15} {'N/A':>10}")
            continue
        delta = (sv - lv) / abs(lv) * 100 if lv != 0 else 0
        better = "BETTER" if (delta < 0) != higher_better else "worse"
        print(f"{label:<30} {lv:>15.2f} {sv:>15.2f} {delta:>+9.1f}%")

    # Controller stats for seed
    if seed_result.controller_stats:
        print(f"\nSeed Controller Stats:")
        for k, v in seed_result.controller_stats.items():
            print(f"  {k}: {v}")

    # Save results
    os.makedirs(output_dir, exist_ok=True)

    comparison = {
        "SignalClaw-Legacy": legacy_summary,
        "SignalClaw-Seed": seed_summary,
    }
    if seed_result.controller_stats:
        comparison["SignalClaw-Seed"]["controller_stats"] = seed_result.controller_stats

    with open(os.path.join(output_dir, "legacy_vs_seed.json"), "w") as f:
        json.dump(comparison, f, indent=2, ensure_ascii=False)

    print(f"\nResults saved to {output_dir}/legacy_vs_seed.json")


if __name__ == "__main__":
    main()
