#!/usr/bin/env python3
"""
多场景多 seed 批量实验运行器。

对每个 (scenario, seed, method) 组合运行 SUMO 仿真，
汇总结果到 summary.csv / summary.json，
并生成跨场景加权排名报告 cross_scenario_report.txt。

用法示例:
    # 默认: 全部7场景 x 5种子 x 全部方法
    python -m signalclaw.scripts.multi_scenario_runner

    # 指定场景、种子、方法
    python -m signalclaw.scripts.multi_scenario_runner \
        --scenarios base morning_peak \
        --seeds 42 123 \
        --methods FixedTime MaxPressure-Canonical

    # 并行 4 进程
    python -m signalclaw.scripts.multi_scenario_runner --parallel 4
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from multiprocessing import Manager
from typing import Any, Dict, List, Optional, Tuple

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from signalclaw.experiments.runner import ExperimentRunner
from signalclaw.scenario.scenario_catalog import ScenarioCatalog
from signalclaw.skills.max_pressure import (
    MaxPressureCyclicAllocation,
    MaxPressureQueueOnly,
    MaxPressureCanonical,
    MaxPressureSwitchLossAware,
    create_max_pressure,
)

logger = logging.getLogger(__name__)


# ======================================================================
# 常量
# ======================================================================

# 项目根目录
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 场景 .sumocfg 文件所在目录
SCENARIO_DIR = os.path.join(PROJECT_DIR, "sumo_scenarios", "chengdu", "generated")

# 默认种子列表
DEFAULT_SEEDS = [42, 123, 456, 789, 1024]

# 全部可用方法名
ALL_METHODS = [
    "FixedTime",
    "MaxPressure-CyclicAllocation",
    "MaxPressure-QueueOnly",
    "MaxPressure-Canonical",
    "MaxPressure-SwitchLossAware",
    "SignalClaw-legacy",
    "SignalClaw-Seed",
    "SignalClaw-Evolved",
]

# 全部 7 个场景名
ALL_SCENARIOS = [
    "base",
    "morning_peak",
    "evening_peak",
    "low_demand",
    "mainroad_imbalance",
    "leftturn_surge",
    "mixed_stress",
]

# summary.csv 的列名
CSV_COLUMNS = [
    "scenario",
    "seed",
    "method",
    "avg_queue",
    "avg_waiting_time",
    "completed_vehicles",
    "throughput_per_hour",
    "avg_travel_time",
    "total_stops",
    "max_queue",
    "max_waiting_time",
    "controller_stats",
]


# ======================================================================
# 构建方法控制器
# ======================================================================

def _build_controller(method_name: str, decision_interval: float = 5.0) -> Any:
    """根据方法名创建控制器实例。

    返回 None 表示 FixedTime（不做控制）。
    对于 SignalClaw-Seed / SignalClaw-Evolved 返回特殊标记字符串，
    因为它们需要通过 run_signalclaw_cohort 路径执行。
    """
    if method_name == "FixedTime":
        return None

    if method_name == "MaxPressure-CyclicAllocation":
        return MaxPressureCyclicAllocation(decision_interval=decision_interval)

    if method_name == "MaxPressure-QueueOnly":
        return MaxPressureQueueOnly(decision_interval=decision_interval)

    if method_name == "MaxPressure-Canonical":
        return MaxPressureCanonical(decision_interval=decision_interval)

    if method_name == "MaxPressure-SwitchLossAware":
        return MaxPressureSwitchLossAware(decision_interval=decision_interval)

    if method_name == "SignalClaw-legacy":
        # 使用 SignalClawSkill（传统 skill 路径）
        from signalclaw.skills.signalclaw_skill import SignalClawSkill
        return SignalClawSkill(decision_interval=decision_interval)

    # SignalClaw-Seed / SignalClaw-Evolved 需要特殊处理（cohort 路径）
    # 返回 None，由 _run_single_experiment 特殊判断
    return None


def _is_cohort_method(method_name: str) -> bool:
    """判断方法是否需要通过 cohort 路径执行。"""
    return method_name in ("SignalClaw-Seed", "SignalClaw-Evolved")


def _cohort_path(method_name: str) -> Optional[str]:
    """获取 cohort 文件路径。"""
    if method_name == "SignalClaw-Seed":
        path = os.path.join(PROJECT_DIR, "artifacts", "skills", "cohorts", "seed_cohort.json")
        if os.path.exists(path):
            return path

    if method_name == "SignalClaw-Evolved":
        # 优先 evolution_archive，其次 skills/cohorts
        for candidate in [
            os.path.join(PROJECT_DIR, "artifacts", "evolution_archive", "evolved_cohort.json"),
            os.path.join(PROJECT_DIR, "artifacts", "skills", "cohorts", "evolved_cohort.json"),
        ]:
            if os.path.exists(candidate):
                return candidate

    return None


def _neighbor_graph_path() -> str:
    """获取邻居图路径。"""
    path = os.path.join(PROJECT_DIR, "artifacts", "topology", "one_hop_neighbors.json")
    return path if os.path.exists(path) else ""


# ======================================================================
# 单次实验执行
# ======================================================================

def _run_single_experiment(
    scenario_name: str,
    seed: int,
    method_name: str,
    sumocfg_path: str,
    decision_interval: float = 5.0,
    sim_duration: float = 3600.0,
    verbose: bool = False,
) -> Dict[str, Any]:
    """运行一次实验，返回指标字典。

    Parameters
    ----------
    scenario_name : str
        场景名称
    seed : int
        随机种子
    method_name : str
        方法名
    sumocfg_path : str
        .sumocfg 文件绝对路径
    decision_interval : float
        决策间隔（秒）
    sim_duration : float
        仿真时长（秒）
    verbose : bool
        是否打印详细信息

    Returns
    -------
    dict
        包含 scenario, seed, method 和所有指标的字典
    """
    runner = ExperimentRunner(
        sumocfg_path=sumocfg_path,
        seed=seed,
        decision_interval=decision_interval,
        sim_duration=sim_duration,
    )

    metrics_result = None

    if _is_cohort_method(method_name):
        # cohort 方法: SignalClaw-Seed / SignalClaw-Evolved
        cpath = _cohort_path(method_name)
        if cpath is None:
            logger.warning(f"Cohort 文件不存在，跳过 {method_name}")
            return _empty_result(scenario_name, seed, method_name, "cohort_not_found")

        ng_path = _neighbor_graph_path()
        try:
            metrics_result = runner.run_signalclaw_cohort(
                cohort_path=cpath,
                neighbor_graph_path=ng_path,
                method_name=method_name,
                verbose=verbose,
            )
        except Exception as e:
            logger.error(f"[{scenario_name}/seed={seed}/{method_name}] 运行失败: {e}")
            return _empty_result(scenario_name, seed, method_name, str(e))

    else:
        # 传统方法 + MaxPressure 变体 + SignalClaw-legacy
        controller = _build_controller(method_name, decision_interval)
        try:
            metrics_result = runner._run_simulation(method_name, controller, verbose=verbose)
        except Exception as e:
            logger.error(f"[{scenario_name}/seed={seed}/{method_name}] 运行失败: {e}")
            return _empty_result(scenario_name, seed, method_name, str(e))

    if metrics_result is None:
        return _empty_result(scenario_name, seed, method_name, "no_result")

    # 从 SimulationMetrics 提取摘要
    summary = metrics_result.summary()
    ctrl_stats = metrics_result.controller_stats

    return {
        "scenario": scenario_name,
        "seed": seed,
        "method": method_name,
        "avg_queue": summary.get("avg_queue"),
        "avg_waiting_time": summary.get("avg_waiting_time"),
        "completed_vehicles": summary.get("completed_vehicles"),
        "throughput_per_hour": summary.get("throughput_per_hour"),
        "avg_travel_time": summary.get("avg_travel_time"),
        "total_stops": summary.get("total_stops"),
        "max_queue": summary.get("max_queue"),
        "max_waiting_time": summary.get("max_waiting_time"),
        "controller_stats": ctrl_stats,
        "error": None,
    }


def _empty_result(
    scenario_name: str, seed: int, method_name: str, error: str
) -> Dict[str, Any]:
    """生成空结果（运行失败时使用）。"""
    return {
        "scenario": scenario_name,
        "seed": seed,
        "method": method_name,
        "avg_queue": None,
        "avg_waiting_time": None,
        "completed_vehicles": None,
        "throughput_per_hour": None,
        "avg_travel_time": None,
        "total_stops": None,
        "max_queue": None,
        "max_waiting_time": None,
        "controller_stats": None,
        "error": error,
    }


# ======================================================================
# 并行 worker（用于 ProcessPoolExecutor）
# ======================================================================

def _worker(args: Tuple) -> Dict[str, Any]:
    """并行 worker: 解包参数并调用 _run_single_experiment。"""
    (scenario_name, seed, method_name, sumocfg_path,
     decision_interval, sim_duration, verbose) = args
    return _run_single_experiment(
        scenario_name=scenario_name,
        seed=seed,
        method_name=method_name,
        sumocfg_path=sumocfg_path,
        decision_interval=decision_interval,
        sim_duration=sim_duration,
        verbose=verbose,
    )


# ======================================================================
# 汇总与报告
# ======================================================================

def _save_summary_csv(results: List[Dict[str, Any]], output_path: str) -> None:
    """保存原始结果到 summary.csv。"""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            # controller_stats 序列化为 JSON 字符串
            if row.get("controller_stats") and isinstance(row["controller_stats"], dict):
                row["controller_stats"] = json.dumps(row["controller_stats"], ensure_ascii=False)
            writer.writerow(row)
    logger.info(f"CSV 已保存: {output_path}")


def _save_summary_json(results: List[Dict[str, Any]], output_path: str) -> None:
    """保存原始结果到 summary.json，附带聚合统计。"""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # 按场景和方法聚合
    agg = _aggregate_results(results)

    data = {
        "raw_results": results,
        "aggregated": agg,
        "meta": {
            "total_runs": len(results),
            "scenarios": sorted(set(r["scenario"] for r in results)),
            "methods": sorted(set(r["method"] for r in results)),
            "seeds": sorted(set(r["seed"] for r in results)),
        },
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"JSON 已保存: {output_path}")


def _aggregate_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按 (scenario, method) 聚合结果：跨 seed 计算均值和标准差。"""
    # 过滤掉失败的运行
    valid = [r for r in results if r.get("error") is None]

    groups: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    for r in valid:
        groups[(r["scenario"], r["method"])].append(r)

    import math

    aggregated = {}
    for (scenario, method), runs in sorted(groups.items()):
        key = f"{scenario}/{method}"
        n = len(runs)
        agg_entry = {"n_seeds": n, "metrics": {}}

        for metric_key in [
            "avg_queue", "avg_waiting_time", "completed_vehicles",
            "throughput_per_hour", "avg_travel_time", "total_stops",
        ]:
            values = [r[metric_key] for r in runs if r.get(metric_key) is not None]
            if not values:
                agg_entry["metrics"][metric_key] = {
                    "mean": None, "std": None, "n": 0,
                }
                continue
            mean = sum(values) / len(values)
            if len(values) > 1:
                variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
                std = math.sqrt(variance)
            else:
                std = 0.0
            agg_entry["metrics"][metric_key] = {
                "mean": round(mean, 4),
                "std": round(std, 4),
                "n": len(values),
            }

        aggregated[key] = agg_entry

    return aggregated


def _generate_cross_scenario_report(
    results: List[Dict[str, Any]],
    output_path: str,
) -> str:
    """生成跨场景加权排名报告。

    使用 ScenarioCatalog 中的 weight 做加权平均。
    """
    import math

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # 获取场景权重
    catalog = ScenarioCatalog.default_catalog(SCENARIO_DIR)
    weights = {e.name: e.weight for e in catalog.scenarios}

    # 过滤有效结果
    valid = [r for r in results if r.get("error") is None]

    # 按 (scenario, method) 聚合
    groups: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    for r in valid:
        groups[(r["scenario"], r["method"])].append(r)

    # 对每个 (scenario, method) 计算跨 seed 均值
    scenario_method_mean: Dict[Tuple[str, str], float] = {}
    for (scenario, method), runs in groups.items():
        values = [r["avg_travel_time"] for r in runs if r.get("avg_travel_time") is not None]
        if values:
            scenario_method_mean[(scenario, method)] = sum(values) / len(values)

    # 获取所有场景和方法
    scenarios_in_data = sorted(set(r["scenario"] for r in valid))
    methods_in_data = sorted(set(r["method"] for r in valid))

    # 所有指标列表（用于详细报告）
    detail_metrics = [
        ("avg_queue", "平均排队长度", False),
        ("avg_waiting_time", "平均等待时间 (s)", False),
        ("completed_vehicles", "完成车辆数", True),
        ("throughput_per_hour", "每小时吞吐量", True),
        ("avg_travel_time", "平均行程时间 (s)", False),
        ("total_stops", "总停车次数", False),
    ]

    lines: List[str] = []
    lines.append("=" * 80)
    lines.append("跨场景对比报告 (Cross-Scenario Comparison Report)")
    lines.append("=" * 80)
    lines.append("")

    # ---- 第一部分: 每个方法在每个场景下的平均表现 ----
    lines.append("一、各方法在各场景下的平均表现（跨 seed 均值）")
    lines.append("-" * 80)

    for method in methods_in_data:
        lines.append(f"\n  [{method}]")
        lines.append(f"  {'场景':<20s} {'seed数':>6s}  "
                     f"{'avg_queue':>10s} {'avg_wait':>10s} "
                     f"{'completed':>10s} {'avg_travel':>10s} {'stops':>10s}")
        lines.append(f"  {'-' * 76}")

        for scenario in scenarios_in_data:
            runs = groups.get((scenario, method), [])
            if not runs:
                lines.append(f"  {scenario:<20s} {'N/A':>6s}  "
                             f"{'--':>10s} {'--':>10s} "
                             f"{'--':>10s} {'--':>10s} {'--':>10s}")
                continue

            n_seeds = len(runs)
            means = {}
            for mk, _, _ in detail_metrics:
                vals = [r[mk] for r in runs if r.get(mk) is not None]
                means[mk] = sum(vals) / len(vals) if vals else None

            def fmt(v, precision=1):
                if v is None:
                    return "--"
                return f"{v:.{precision}f}"

            lines.append(
                f"  {scenario:<20s} {n_seeds:>6d}  "
                f"{fmt(means.get('avg_queue')):>10s} {fmt(means.get('avg_waiting_time')):>10s} "
                f"{fmt(means.get('completed_vehicles'), 0):>10s} "
                f"{fmt(means.get('avg_travel_time')):>10s} "
                f"{fmt(means.get('total_stops'), 0):>10s}"
            )
        lines.append("")

    # ---- 第二部分: 综合排名（加权平均） ----
    lines.append("")
    lines.append("二、综合排名（跨场景加权平均，权重来自 ScenarioCatalog）")
    lines.append("-" * 80)

    total_weight = sum(weights.get(s, 1.0) for s in scenarios_in_data)

    # 对每个指标计算加权平均
    weighted_scores: Dict[str, Dict[str, float]] = {}
    for method in methods_in_data:
        weighted_scores[method] = {}
        for mk, _, higher_better in detail_metrics:
            wsum = 0.0
            wden = 0.0
            for scenario in scenarios_in_data:
                runs = groups.get((scenario, method), [])
                if not runs:
                    continue
                vals = [r[mk] for r in runs if r.get(mk) is not None]
                if not vals:
                    continue
                mean_val = sum(vals) / len(vals)
                w = weights.get(scenario, 1.0)
                wsum += mean_val * w
                wden += w
            if wden > 0:
                weighted_scores[method][mk] = wsum / wden
            else:
                weighted_scores[method][mk] = None

    # 按 avg_travel_time 排序（越小越好）
    methods_sorted = sorted(
        methods_in_data,
        key=lambda m: weighted_scores[m].get("avg_travel_time", float("inf")) or float("inf"),
    )

    lines.append(f"\n  {'排名':>4s}  {'方法':<35s}  "
                 f"{'avg_queue':>10s} {'avg_wait':>10s} "
                 f"{'completed':>10s} {'avg_travel':>10s} {'stops':>10s}")
    lines.append(f"  {'-' * 89}")

    for rank, method in enumerate(methods_sorted, 1):
        scores = weighted_scores[method]
        lines.append(
            f"  {rank:>4d}  {method:<35s}  "
            f"{fmt(scores.get('avg_queue')):>10s} "
            f"{fmt(scores.get('avg_waiting_time')):>10s} "
            f"{fmt(scores.get('completed_vehicles'), 0):>10s} "
            f"{fmt(scores.get('avg_travel_time')):>10s} "
            f"{fmt(scores.get('total_stops'), 0):>10s}"
        )

    # ---- 第三部分: 场景权重参考 ----
    lines.append("")
    lines.append("三、场景权重")
    lines.append("-" * 80)
    for scenario in scenarios_in_data:
        w = weights.get(scenario, 1.0)
        pct = w / total_weight * 100 if total_weight > 0 else 0
        lines.append(f"  {scenario:<20s} weight={w:.1f}  ({pct:.1f}%)")

    lines.append("")
    lines.append("=" * 80)
    lines.append("报告结束")
    lines.append("=" * 80)

    report_text = "\n".join(lines)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    logger.info(f"报告已保存: {output_path}")

    return report_text


# ======================================================================
# 主流程
# ======================================================================

def build_experiment_matrix(
    scenarios: List[str],
    seeds: List[int],
    methods: List[str],
) -> List[Tuple[str, int, str, str]]:
    """构建实验矩阵: [(scenario, seed, method, sumocfg_path), ...]。"""
    matrix = []
    for scenario in scenarios:
        sumocfg_path = os.path.join(SCENARIO_DIR, f"chengdu_{scenario}.sumocfg")
        if not os.path.exists(sumocfg_path):
            logger.warning(f"场景 .sumocfg 不存在，跳过: {sumocfg_path}")
            continue
        for seed in seeds:
            for method in methods:
                # 跳过 cohort 文件不存在的方法
                if _is_cohort_method(method) and _cohort_path(method) is None:
                    logger.warning(f"Cohort 文件不存在，跳过方法: {method}")
                    continue
                matrix.append((scenario, seed, method, sumocfg_path))
    return matrix


def run_experiments(
    scenarios: List[str],
    seeds: List[int],
    methods: List[str],
    output_dir: str,
    parallel: int = 1,
    decision_interval: float = 5.0,
    sim_duration: float = 3600.0,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """运行全部实验组合。"""
    matrix = build_experiment_matrix(scenarios, seeds, methods)
    total = len(matrix)

    if total == 0:
        logger.error("没有有效的实验组合")
        return []

    print(f"实验矩阵: {len(scenarios)} 场景 x {len(seeds)} 种子 x "
          f"{len(methods)} 方法 = {total} 次运行")
    print(f"并行进程: {parallel}")
    print(f"输出目录: {output_dir}")
    print("")

    # 构建 worker 参数
    worker_args = [
        (scenario, seed, method, sumocfg_path,
         decision_interval, sim_duration, verbose)
        for scenario, seed, method, sumocfg_path in matrix
    ]

    results: List[Dict[str, Any]] = []
    start_time = time.time()

    if parallel <= 1:
        # 串行执行
        for i, args in enumerate(worker_args):
            scenario, seed, method, _ = args[:4]
            print(f"[{i + 1}/{total}] {scenario} / seed={seed} / {method} ...")
            try:
                result = _worker(args)
                results.append(result)
                _print_result_line(result)
            except Exception as e:
                logger.error(f"[{i + 1}/{total}] 异常: {e}")
                results.append(_empty_result(scenario, seed, method, str(e)))
    else:
        # 并行执行
        with ProcessPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(_worker, args): args
                for args in worker_args
            }
            for i, future in enumerate(as_completed(futures)):
                args = futures[future]
                scenario, seed, method, _ = args[:4]
                try:
                    result = future.result()
                    results.append(result)
                    print(f"[{i + 1}/{total}] {scenario}/seed={seed}/{method} 完成")
                    _print_result_line(result)
                except Exception as e:
                    logger.error(f"[{i + 1}/{total}] {scenario}/seed={seed}/{method} 异常: {e}")
                    results.append(_empty_result(scenario, seed, method, str(e)))

    elapsed = time.time() - start_time
    print(f"\n全部实验完成，耗时 {elapsed:.1f}s")

    # 汇总输出
    os.makedirs(output_dir, exist_ok=True)

    _save_summary_csv(results, os.path.join(output_dir, "summary.csv"))
    _save_summary_json(results, os.path.join(output_dir, "summary.json"))
    _generate_cross_scenario_report(results, os.path.join(output_dir, "cross_scenario_report.txt"))

    return results


def _print_result_line(result: Dict[str, Any]) -> None:
    """打印单次结果的摘要行。"""
    if result.get("error"):
        print(f"    失败: {result['error']}")
        return
    cv = result.get("completed_vehicles", "N/A")
    att = result.get("avg_travel_time")
    aq = result.get("avg_queue")
    aw = result.get("avg_waiting_time")
    ts = result.get("total_stops")

    def _f(v, p=1):
        return f"{v:.{p}f}" if v is not None else "N/A"

    print(f"    vehicles={cv}, avg_travel={_f(att)}s, "
          f"avg_queue={_f(aq)}, avg_wait={_f(aw)}s, stops={ts}")


# ======================================================================
# CLI
# ======================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="多场景多 seed 批量实验运行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
可用场景: {', '.join(ALL_SCENARIOS)}
可用方法: {', '.join(ALL_METHODS)}
默认种子: {', '.join(str(s) for s in DEFAULT_SEEDS)}

示例:
  # 全部默认
  python -m signalclaw.scripts.multi_scenario_runner

  # 指定场景和方法
  python -m signalclaw.scripts.multi_scenario_runner \\
      --scenarios base morning_peak \\
      --methods FixedTime MaxPressure-Canonical SignalClaw-Seed

  # 指定种子和并行度
  python -m signalclaw.scripts.multi_scenario_runner \\
      --seeds 42 123 456 --parallel 4
""",
    )

    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=None,
        choices=ALL_SCENARIOS,
        help="选择场景（默认全部7个）",
    )
    parser.add_argument(
        "--seeds",
        nargs="+",
        type=int,
        default=DEFAULT_SEEDS,
        help=f"随机种子列表（默认 {DEFAULT_SEEDS}）",
    )
    parser.add_argument(
        "--methods",
        nargs="+",
        default=None,
        help="选择实验方法（默认全部可用方法）",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(PROJECT_DIR, "results", "multi_scenario"),
        help="输出目录（默认 results/multi_scenario/）",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="并行进程数（默认 1，串行）",
    )
    parser.add_argument(
        "--decision-interval",
        type=float,
        default=5.0,
        help="决策间隔秒数（默认 5.0）",
    )
    parser.add_argument(
        "--sim-duration",
        type=float,
        default=3600.0,
        help="仿真时长秒数（默认 3600.0）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印详细仿真日志",
    )

    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args = parse_args()

    scenarios = args.scenarios or ALL_SCENARIOS
    methods = args.methods or ALL_METHODS

    # 过滤掉 cohort 文件不存在的方法，提前警告
    available_methods = []
    for m in methods:
        if _is_cohort_method(m):
            cpath = _cohort_path(m)
            if cpath is None:
                print(f"警告: {m} 的 cohort 文件不存在，已跳过")
                continue
        available_methods.append(m)

    if not available_methods:
        print("错误: 没有可用的实验方法")
        sys.exit(1)

    print(f"场景: {scenarios}")
    print(f"种子: {args.seeds}")
    print(f"方法: {available_methods}")
    print("")

    results = run_experiments(
        scenarios=scenarios,
        seeds=args.seeds,
        methods=available_methods,
        output_dir=args.output_dir,
        parallel=args.parallel,
        decision_interval=args.decision_interval,
        sim_duration=args.sim_duration,
        verbose=args.verbose,
    )

    # 打印简要汇总
    n_success = sum(1 for r in results if r.get("error") is None)
    n_fail = sum(1 for r in results if r.get("error") is not None)
    print(f"\n运行统计: 成功 {n_success}, 失败 {n_fail}, 共 {len(results)} 次")
    print(f"结果保存在: {args.output_dir}")


if __name__ == "__main__":
    main()
