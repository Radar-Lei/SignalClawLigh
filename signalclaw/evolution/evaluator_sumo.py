"""SUMOEvaluator - T-SUMO 离线评估器。

在全场景 SUMO 仿真中对单个路口的候选 Skill 进行真实交通性能评估。
仅在 AST + Replay 都通过后才调用此模块。

评估流程：
1. 将候选 Skill 代码注入到 cohort 中替换目标路口的对应 skill
2. 用 OnlineController 运行完整的 SUMO 仿真
3. 收集该路口的交通指标（等待时间、排队长度、吞吐量等）
4. 多种子评估取平均
5. 评估完毕恢复原 skill

Sealed 模式（SealedSUMOEvaluator）：
- 用于 deployable champion 的必需条件
- 做 paired evaluation：candidate vs incumbent
- 使用相同 route seed、demand seed、scenario hash
- 返回结构化的对比结果（delta 指标）
- 支持多 seed 重复评估
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import tempfile
import time
import traceback
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from signalclaw.adapters.sumo_traci import SumoTraCIAdapter
from signalclaw.scenario.scenario_catalog import ScenarioCatalog
from signalclaw.core.constraints import NetworkConstraints
from signalclaw.core.state import (
    CyclePlan,
    IntersectionObservation,
    NetworkObservation,
    PhaseCommand,
    PhaseObservation,
)
from signalclaw.execution.online_controller import OnlineController
from signalclaw.execution.phase_command_executor import PhaseCommandExecutor
from signalclaw.network.neighbor_graph import NeighborGraph
from signalclaw.skills.cohort import SkillCohort
from signalclaw.skills.loader import _dynamic_load

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SUMOEvalReport:
    """SUMO 离线评估报告。"""

    candidate_id: str
    crossing_id: str
    skill_type: str
    passed: bool
    score: float  # 综合评分（越低越好）
    metrics: Dict[str, float] = field(default_factory=dict)
    violations: List[str] = field(default_factory=list)
    failure_cases: List[dict] = field(default_factory=list)
    sim_duration: float = 0.0  # 实际仿真时长
    seed: int = 42
    n_seeds: int = 1
    per_seed_metrics: List[Dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SUMOEvalReport":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Paired Evaluation Report
# ---------------------------------------------------------------------------

@dataclass
class PairedEvalReport:
    """Sealed paired evaluation 的结构化对比报告。

    对 candidate 和 incumbent 使用完全相同的 seed、场景进行 SUMO 仿真，
    然后比较关键指标，判定 candidate 是否通过非退化门槛。
    """

    candidate_id: str
    incumbent_id: str
    crossing_id: str
    skill_type: str

    # incumbent 和 candidate 的各自指标
    incumbent_metrics: Dict[str, float] = field(default_factory=dict)
    candidate_metrics: Dict[str, float] = field(default_factory=dict)

    # delta 指标（candidate - incumbent），负值表示改善
    delta: Dict[str, float] = field(default_factory=dict)

    # 评估使用的 seeds
    seeds_used: List[int] = field(default_factory=list)
    n_seeds: int = 1

    # 非退化门槛判定
    passed: bool = False
    rejection_reason: str = ""

    # 每个门槛的详细检查结果
    gate_details: Dict[str, dict] = field(default_factory=dict)

    # 各自的完整 SUMO 报告（用于存档）
    incumbent_report: Optional[Dict] = None
    candidate_report: Optional[Dict] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PairedEvalReport":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Non-degradation gates (thresholds for paired evaluation)
# ---------------------------------------------------------------------------

NON_DEGRADATION_GATES = {
    "completed_vehicles": {
        "direction": "lower_bound",   # candidate 不能低于 incumbent 的 X%
        "threshold_ratio": 0.99,       # 不低于 incumbent 的 99%
        "metric_key": "completed_vehicles",  # 对应 metrics 中的键名
        "description": "completed_vehicles 不低于 incumbent 的 99%",
    },
    "avg_queue": {
        "direction": "upper_bound",    # candidate 不能高于 incumbent 的 X%
        "threshold_ratio": 1.03,       # 不超过 incumbent 的 103%
        "metric_key": "mean_queue",
        "description": "avg_queue 不超过 incumbent 的 103%",
    },
    "avg_waiting_time": {
        "direction": "upper_bound",
        "threshold_ratio": 1.03,       # 不超过 incumbent 的 103%
        "metric_key": "mean_waiting",
        "description": "avg_waiting_time 不超过 incumbent 的 103%",
    },
    "safety_violations": {
        "direction": "absolute_zero",  # 必须为 0
        "metric_key": "safety_overrides",
        "description": "safety_violations 必须为 0",
    },
    "phase_starvation": {
        "direction": "absolute_zero",  # 必须为 0
        "metric_key": "phase_starvation_count",
        "description": "phase_starvation 必须为 0",
    },
}


# ---------------------------------------------------------------------------
# Metrics thresholds（用于判定 pass/fail）
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLDS: Dict[str, Tuple[float, str]] = {
    # metric_name -> (threshold, description)
    # 如果指标超过阈值则记为 violation
    "mean_waiting": (120.0, "平均等待时间超过 120 秒"),
    "max_queue": (80.0, "最大排队长度超过 80 辆"),
    "mean_queue": (30.0, "平均排队长度超过 30 辆"),
    "spillback_ratio": (0.5, "溢出事件占比超过 50%"),
    "phase_starvation_ratio": (0.3, "相位饥饿占比超过 30%"),
    "safety_override_ratio": (0.2, "安全覆写占比超过 20%"),
}


# ---------------------------------------------------------------------------
# SUMOEvaluator
# ---------------------------------------------------------------------------

class SUMOEvaluator:
    """T-SUMO 离线评估器 — 对单个路口的候选 Skill 进行 SUMO 仿真评估。

    在全场景中评估，仅替换目标路口的 skill，其他路口保持原有 skill。
    支持 multiple-seed 评估以减少随机性影响。
    """

    def __init__(
        self,
        sumocfg_path: str,
        neighbor_graph: NeighborGraph,
        constraints: NetworkConstraints,
        eval_duration: float = 600.0,
        n_seeds: int = 3,
        step_length: float = 1.0,
        decision_interval: float = 5.0,
        warmup_steps: int = 100,
        thresholds: Optional[Dict[str, Tuple[float, str]]] = None,
    ):
        """
        Parameters
        ----------
        sumocfg_path : str
            SUMO 配置文件路径 (.sumocfg)
        neighbor_graph : NeighborGraph
            邻居拓扑图
        constraints : NetworkConstraints
            网络约束
        eval_duration : float
            评估仿真时长（秒），默认 600 秒（10 分钟）
        n_seeds : int
            用几个不同种子评估，默认 3
        step_length : float
            SUMO 仿真步长（秒），默认 1.0
        decision_interval : float
            相位决策间隔（秒），默认 5.0
        warmup_steps : int
            热身步数（这些步的指标不计入统计），默认 100
        thresholds : dict, optional
            自定义通过阈值
        """
        self.sumocfg_path = sumocfg_path
        self.neighbor_graph = neighbor_graph
        self.constraints = constraints
        self.eval_duration = eval_duration
        self.n_seeds = n_seeds
        self.step_length = step_length
        self.decision_interval = decision_interval
        self.warmup_steps = warmup_steps
        self.thresholds = thresholds or _DEFAULT_THRESHOLDS

    # ======================================================================
    # Public API
    # ======================================================================

    def evaluate_candidate(
        self,
        candidate_code: str,
        skill_type: str,
        crossing_id: str,
        cohort: SkillCohort,
        seed: int = 42,
    ) -> SUMOEvalReport:
        """评估单个候选 Skill（单种子）。

        流程：
        1. 动态加载候选代码为一个 skill 对象
        2. 在 cohort 中临时替换指定路口的 skill
        3. 运行 SUMO 仿真，收集该路口的指标
        4. 恢复原 skill
        """
        candidate_id = f"sumo_{crossing_id}_{skill_type}_{uuid.uuid4().hex[:8]}"

        # 动态加载候选 skill
        try:
            candidate_skill = _dynamic_load(candidate_code, skill_type)
        except Exception as e:
            return SUMOEvalReport(
                candidate_id=candidate_id,
                crossing_id=crossing_id,
                skill_type=skill_type,
                passed=False,
                score=float("inf"),
                violations=[f"候选代码加载失败: {e}"],
                seed=seed,
            )

        # 保存原始 skill 的引用
        original_skill = self._get_original_skill(cohort, crossing_id, skill_type)

        # 临时替换
        self._inject_skill(cohort, crossing_id, skill_type, candidate_skill)

        try:
            metrics = self._run_single_eval(cohort, crossing_id, seed)
        except Exception as e:
            logger.error(f"SUMO 评估异常: {e}\n{traceback.format_exc()}")
            return SUMOEvalReport(
                candidate_id=candidate_id,
                crossing_id=crossing_id,
                skill_type=skill_type,
                passed=False,
                score=float("inf"),
                violations=[f"SUMO 仿真异常: {e}"],
                failure_cases=[{"exception": str(e), "traceback": traceback.format_exc()}],
                seed=seed,
            )
        finally:
            # 恢复原 skill
            self._restore_skill(cohort, crossing_id, skill_type, original_skill)

        # 计算评分和判定 pass/fail
        score, violations = self._compute_score(metrics)

        passed = len(violations) == 0
        sim_duration = metrics.get("sim_duration", 0.0)

        return SUMOEvalReport(
            candidate_id=candidate_id,
            crossing_id=crossing_id,
            skill_type=skill_type,
            passed=passed,
            score=round(score, 4),
            metrics=metrics,
            violations=violations,
            sim_duration=sim_duration,
            seed=seed,
            n_seeds=1,
            per_seed_metrics=[metrics],
        )

    def evaluate_multi_seed(
        self,
        candidate_code: str,
        skill_type: str,
        crossing_id: str,
        cohort: SkillCohort,
        seeds: Optional[List[int]] = None,
    ) -> SUMOEvalReport:
        """多种子评估，取指标平均。

        Parameters
        ----------
        candidate_code : str
            候选 skill 代码
        skill_type : str
            "cycle" 或 "phase"
        crossing_id : str
            目标路口 ID
        cohort : SkillCohort
            当前 skill 集合
        seeds : list[int], optional
            自定义种子列表；如果不提供则自动生成 n_seeds 个

        Returns
        -------
        SUMOEvalReport
            多种子聚合报告
        """
        if seeds is None:
            seeds = [42 + i * 7 for i in range(self.n_seeds)]

        candidate_id = f"sumo_ms_{crossing_id}_{skill_type}_{uuid.uuid4().hex[:8]}"

        # 动态加载候选 skill
        try:
            candidate_skill = _dynamic_load(candidate_code, skill_type)
        except Exception as e:
            return SUMOEvalReport(
                candidate_id=candidate_id,
                crossing_id=crossing_id,
                skill_type=skill_type,
                passed=False,
                score=float("inf"),
                violations=[f"候选代码加载失败: {e}"],
                n_seeds=len(seeds),
            )

        # 保存原始 skill 的引用
        original_skill = self._get_original_skill(cohort, crossing_id, skill_type)

        # 临时替换
        self._inject_skill(cohort, crossing_id, skill_type, candidate_skill)

        all_metrics: List[Dict[str, float]] = []
        all_violations: List[str] = []

        try:
            for seed in seeds:
                try:
                    metrics = self._run_single_eval(cohort, crossing_id, seed)
                    all_metrics.append(metrics)
                except Exception as e:
                    logger.warning(
                        f"SUMO 评估 seed={seed} 异常: {e}"
                    )
                    all_violations.append(f"seed={seed} 仿真异常: {e}")
        finally:
            # 恢复原 skill
            self._restore_skill(cohort, crossing_id, skill_type, original_skill)

        if not all_metrics:
            return SUMOEvalReport(
                candidate_id=candidate_id,
                crossing_id=crossing_id,
                skill_type=skill_type,
                passed=False,
                score=float("inf"),
                violations=all_violations or ["所有 seed 评估都失败"],
                n_seeds=len(seeds),
            )

        # 聚合多 seed 指标（取平均）
        avg_metrics = self._aggregate_metrics(all_metrics)

        # 计算评分
        score, threshold_violations = self._compute_score(avg_metrics)
        all_violations.extend(threshold_violations)

        passed = len(all_violations) == 0
        avg_sim_duration = avg_metrics.get("sim_duration", 0.0)

        return SUMOEvalReport(
            candidate_id=candidate_id,
            crossing_id=crossing_id,
            skill_type=skill_type,
            passed=passed,
            score=round(score, 4),
            metrics=avg_metrics,
            violations=all_violations,
            sim_duration=avg_sim_duration,
            seed=seeds[0],
            n_seeds=len(seeds),
            per_seed_metrics=all_metrics,
        )

    # ======================================================================
    # Core simulation
    # ======================================================================

    def _run_single_eval(
        self,
        cohort: SkillCohort,
        crossing_id: str,
        seed: int,
    ) -> Dict[str, float]:
        """运行单次 SUMO 仿真，收集目标路口的指标。

        Returns
        -------
        dict
            路口指标字典
        """
        adapter = SumoTraCIAdapter(
            sumocfg_path=self.sumocfg_path,
            use_gui=False,
            seed=seed,
            step_length=self.step_length,
        )

        # 累积指标
        step_waiting_times: List[float] = []
        step_queue_lengths: List[float] = []
        # step_throughputs removed (INFO-01): was unused
        max_queue = 0.0
        safety_overrides = 0
        phase_starvation_count = 0
        spillback_events = 0
        total_steps = 0
        prev_phase_id: Optional[int] = None
        phase_appearances: Dict[int, int] = defaultdict(int)
        total_safety_clips = 0
        throughput_window: List[float] = []
        arrived_window: List[float] = []

        try:
            adapter.start()

            # 获取所有 TLS ID
            tls_ids = adapter.get_tls_ids()
            if crossing_id not in tls_ids:
                raise ValueError(
                    f"目标路口 {crossing_id} 不在 SUMO 网络中。"
                    f" 可用路口: {tls_ids[:5]}..."
                )

            # 创建 OnlineController
            controller = OnlineController(
                cohort=cohort,
                neighbor_graph=self.neighbor_graph,
                constraints=self.constraints,
                decision_interval=self.decision_interval,
                sim_step_length=self.step_length,
            )

            # 创建 PhaseCommandExecutor（跨 step 保持 pending 状态）
            phase_executor = PhaseCommandExecutor.for_adapter(
                adapter, self.constraints
            )

            # 运行仿真主循环
            max_steps = int(self.eval_duration / self.step_length)
            sim_time = 0.0

            for step_num in range(max_steps):
                sim_time = adapter.step()
                total_steps += 1

                # 观测所有路口
                all_obs = adapter.observe_network()

                # 处理 pending switch durations（过渡完成后设置 duration）
                phase_executor.process_pending_switches(tls_ids)

                # 对每个路口执行 controller step
                for tls_id in tls_ids:
                    cmd = controller.step(tls_id, sim_time, all_obs)
                    if cmd is not None:
                        phase_executor.apply(cmd, tls_id)

                # 跳过热身阶段
                if step_num < self.warmup_steps:
                    continue

                # 收集目标路口指标
                if crossing_id in all_obs:
                    obs = all_obs[crossing_id]

                    # 等待时间
                    total_wait = 0.0
                    for p in obs.phases.values():
                        total_wait += p.waiting_time
                    avg_wait = total_wait / max(len(obs.phases), 1)
                    step_waiting_times.append(avg_wait)

                    # 排队长度
                    total_queue = sum(p.queue for p in obs.phases.values())
                    step_queue_lengths.append(total_queue)
                    max_queue = max(max_queue, total_queue)

                    # 相位覆盖率
                    current_phase = obs.current_phase_id
                    phase_appearances[current_phase] += 1

                    if prev_phase_id is not None and current_phase != prev_phase_id:
                        pass  # 正常相位切换
                    prev_phase_id = current_phase

                # 收集 throughput（目标路口的通过量 + 全网完成行程车辆数）
                intersection_tp = adapter.get_intersection_throughput(crossing_id)
                throughput_window.append(intersection_tp)
                completed_count = len(adapter.get_arrived_vehicles())
                arrived_window.append(completed_count)

                # 收集 safety clip 计数
                total_safety_clips = controller.stats.safety_clip_count

            # ---- 计算汇总指标 ----
            n_valid = max(len(step_waiting_times), 1)

            # BLOCKER-01: throughput 用 avg_per_step 而不是 sum，避免重复计数
            avg_throughput_per_step = (
                sum(throughput_window) / max(len(throughput_window), 1)
                if throughput_window else 0.0
            )

            # BLOCKER-02: effective_steps 排除 warmup 阶段
            effective_steps = max(total_steps - self.warmup_steps, 1)

            # BLOCKER-03: _count_phase_starvation 只调用一次，结果复用
            phase_starvation_count = self._count_phase_starvation(
                phase_appearances, effective_steps
            )

            metrics = {
                "mean_waiting": sum(step_waiting_times) / n_valid if step_waiting_times else 0.0,
                "mean_queue": sum(step_queue_lengths) / n_valid if step_queue_lengths else 0.0,
                "max_queue": max_queue,
                "throughput": avg_throughput_per_step,
                "intersection_throughput": avg_throughput_per_step,
                "completed_vehicles": sum(arrived_window),
                "avg_throughput_per_step": avg_throughput_per_step,
                "safety_overrides": float(total_safety_clips),
                "total_steps": float(total_steps),
                "sim_duration": sim_time,
                # 相位饥饿：如果有相位几乎没出现过
                "phase_starvation_count": phase_starvation_count,
                # 溢出事件：下游排队过高的步数占比
                "spillback_events": self._count_spillback_from_obs(adapter, crossing_id),
                # 安全覆写比（分母排除 warmup）
                "safety_override_ratio": total_safety_clips / effective_steps,
                # 相位饥饿比
                "phase_starvation_ratio": phase_starvation_count / max(len(phase_appearances), 1),
                # 溢出比
                "spillback_ratio": 0.0,  # 需要从步骤数据计算
            }

            # 从排队数据估算 spillback ratio
            if step_queue_lengths:
                high_queue_steps = sum(1 for q in step_queue_lengths if q > 40.0)
                metrics["spillback_ratio"] = high_queue_steps / len(step_queue_lengths)

            return metrics

        finally:
            adapter.close()

    # ======================================================================
    # Metrics computation
    # ======================================================================

    def _compute_score(
        self, metrics: Dict[str, float]
    ) -> Tuple[float, List[str]]:
        """计算综合评分和违规列表。

        评分 = 加权求和，越低越好。

        Returns
        -------
        (score, violations)
        """
        violations: List[str] = []
        score = 0.0

        # 加权评分
        score += metrics.get("mean_waiting", 0.0) * 0.30
        score += metrics.get("mean_queue", 0.0) * 0.25
        score += metrics.get("max_queue", 0.0) * 0.10
        score -= metrics.get("avg_throughput_per_step", 0.0) * 0.15  # 吞吐量越高越好
        score += metrics.get("safety_override_ratio", 0.0) * 50.0  # 安全覆写惩罚
        score += metrics.get("phase_starvation_ratio", 0.0) * 30.0  # 相位饥饿惩罚
        score += metrics.get("spillback_ratio", 0.0) * 40.0  # 溢出惩罚

        # 检查阈值违规
        for metric_name, (threshold, desc) in self.thresholds.items():
            value = metrics.get(metric_name, 0.0)
            if value > threshold:
                violations.append(
                    f"{metric_name}={value:.2f} 超过阈值 {threshold:.2f}: {desc}"
                )

        return score, violations

    def _aggregate_metrics(
        self, all_metrics: List[Dict[str, float]]
    ) -> Dict[str, float]:
        """聚合多 seed 指标（取平均）。"""
        if not all_metrics:
            return {}

        keys = set()
        for m in all_metrics:
            keys.update(m.keys())

        avg: Dict[str, float] = {}
        for key in keys:
            values = [m.get(key, 0.0) for m in all_metrics if key in m]
            if values:
                avg[key] = sum(values) / len(values)

        return avg

    # ======================================================================
    # Skill injection / restoration
    # ======================================================================

    def _get_original_skill(
        self, cohort: SkillCohort, crossing_id: str, skill_type: str
    ) -> Any:
        """获取 cohort 中原有的 skill 对象。"""
        cache_key = f"{crossing_id}:{skill_type}"
        return cohort._cache.get(cache_key)

    def _inject_skill(
        self,
        cohort: SkillCohort,
        crossing_id: str,
        skill_type: str,
        skill_obj: Any,
    ) -> None:
        """在 cohort 中临时替换目标路口的 skill。"""
        cache_key = f"{crossing_id}:{skill_type}"
        cohort._cache[cache_key] = skill_obj

    def _restore_skill(
        self,
        cohort: SkillCohort,
        crossing_id: str,
        skill_type: str,
        original_skill: Any,
    ) -> None:
        """恢复 cohort 中的原始 skill。"""
        cache_key = f"{crossing_id}:{skill_type}"
        if original_skill is not None:
            cohort._cache[cache_key] = original_skill
        else:
            cohort._cache.pop(cache_key, None)

    # ======================================================================
    # Helper metrics
    # ======================================================================

    @staticmethod
    def _count_phase_starvation(
        phase_appearances: Dict[int, int],
        total_steps: int,
    ) -> int:
        """统计出现相位饥饿的相位数量。

        如果某个相位出现次数不到总步数的 5%，则认为是饥饿。
        """
        if total_steps <= 0:
            return 0

        threshold = total_steps * 0.05
        starved = 0
        for phase_id, count in phase_appearances.items():
            if count < threshold:
                starved += 1
        return starved

    @staticmethod
    def _count_spillback_from_obs(
        adapter: SumoTraCIAdapter,
        crossing_id: str,
    ) -> float:
        """从最后一次观测估算溢出事件（粗略）。"""
        try:
            obs = adapter.observe_intersection(crossing_id)
            spillback = 0.0
            for edge, q in obs.downstream_queue.items():
                if q > 20.0:
                    spillback += 1.0
            return spillback
        except Exception:
            return 0.0

    # ======================================================================
    # Multi-scenario evaluation
    # ======================================================================

    def evaluate_multi_scenario(
        self,
        candidate_code: str,
        skill_type: str,
        crossing_id: str,
        cohort: SkillCohort,
        scenario_catalog: ScenarioCatalog,
        n_seeds: int = 2,
    ) -> SUMOEvalReport:
        """在多个场景下评估候选 Skill。

        对 scenario_catalog 中的每个场景，使用该场景的 .sumocfg
        运行评估，然后按场景权重计算加权综合分数。

        Parameters
        ----------
        candidate_code : str
            候选 skill 代码
        skill_type : str
            "cycle" 或 "phase"
        crossing_id : str
            目标路口 ID
        cohort : SkillCohort
            当前 skill 集合
        scenario_catalog : ScenarioCatalog
            场景目录（包含多个场景条目和权重）
        n_seeds : int
            每个场景使用几个种子评估，默认 2

        Returns
        -------
        SUMOEvalReport
            多场景聚合报告
        """
        candidate_id = (
            f"sumo_multi_{crossing_id}_{skill_type}_{uuid.uuid4().hex[:8]}"
        )

        # 动态加载候选 skill
        try:
            candidate_skill = _dynamic_load(candidate_code, skill_type)
        except Exception as e:
            return SUMOEvalReport(
                candidate_id=candidate_id,
                crossing_id=crossing_id,
                skill_type=skill_type,
                passed=False,
                score=float("inf"),
                violations=[f"候选代码加载失败: {e}"],
                n_seeds=n_seeds,
            )

        # 保存原始 skill 的引用
        original_skill = self._get_original_skill(cohort, crossing_id, skill_type)

        # 临时替换
        self._inject_skill(cohort, crossing_id, skill_type, candidate_skill)

        scenario_results: List[Dict[str, float]] = []
        scenario_weights: List[float] = []
        all_violations: List[str] = []

        try:
            for entry in scenario_catalog:
                if not entry.sumocfg_file or not os.path.exists(entry.sumocfg_file):
                    logger.warning(
                        f"场景 '{entry.name}' 的 sumocfg 文件不存在，跳过"
                    )
                    all_violations.append(
                        f"场景 '{entry.name}' sumocfg 文件缺失"
                    )
                    continue

                logger.info(
                    f"多场景评估: 场景='{entry.name}' "
                    f"weight={entry.weight} n_seeds={n_seeds}"
                )

                # 临时替换 sumocfg 路径
                original_sumocfg = self.sumocfg_path
                self.sumocfg_path = entry.sumocfg_file

                seeds = [42 + i * 7 for i in range(n_seeds)]
                entry_metrics: List[Dict[str, float]] = []

                try:
                    for seed in seeds:
                        try:
                            metrics = self._run_single_eval(
                                cohort, crossing_id, seed
                            )
                            entry_metrics.append(metrics)
                        except Exception as e:
                            logger.warning(
                                f"场景 '{entry.name}' seed={seed} 评估异常: {e}"
                            )
                            all_violations.append(
                                f"场景 '{entry.name}' seed={seed} 异常: {e}"
                            )
                finally:
                    # 恢复 sumocfg 路径
                    self.sumocfg_path = original_sumocfg

                if entry_metrics:
                    avg_metrics = self._aggregate_metrics(entry_metrics)
                    avg_metrics["_scenario_name"] = hash(entry.name)  # type: ignore[assignment]
                    scenario_results.append(avg_metrics)
                    scenario_weights.append(entry.weight)

        finally:
            # 恢复原 skill
            self._restore_skill(cohort, crossing_id, skill_type, original_skill)

        if not scenario_results:
            return SUMOEvalReport(
                candidate_id=candidate_id,
                crossing_id=crossing_id,
                skill_type=skill_type,
                passed=False,
                score=float("inf"),
                violations=all_violations or ["所有场景评估都失败"],
                n_seeds=n_seeds,
            )

        # 计算跨场景加权分数
        weighted_score = self._compute_weighted_score(
            scenario_results, scenario_weights
        )

        # 聚合所有场景指标（加权平均）
        aggregated = self._aggregate_weighted_metrics(
            scenario_results, scenario_weights
        )

        # 检查阈值违规（基于聚合指标）
        _, threshold_violations = self._compute_score(aggregated)
        all_violations.extend(threshold_violations)

        passed = len(all_violations) == 0

        return SUMOEvalReport(
            candidate_id=candidate_id,
            crossing_id=crossing_id,
            skill_type=skill_type,
            passed=passed,
            score=round(weighted_score, 4),
            metrics=aggregated,
            violations=all_violations,
            sim_duration=aggregated.get("sim_duration", 0.0),
            n_seeds=n_seeds,
            per_seed_metrics=scenario_results,
        )

    def _compute_weighted_score(
        self,
        scenario_results: List[Dict[str, float]],
        weights: List[float],
    ) -> float:
        """计算跨场景加权分数。

        对每个场景分别计算 score，然后按权重加权平均。

        Parameters
        ----------
        scenario_results : list[dict]
            每个场景的指标字典
        weights : list[float]
            每个场景的权重

        Returns
        -------
        float
            加权综合分数（越低越好）
        """
        if len(scenario_results) != len(weights):
            logger.warning(
                f"场景结果数({len(scenario_results)})与权重数({len(weights)})不匹配"
            )
            n = min(len(scenario_results), len(weights))
            scenario_results = scenario_results[:n]
            weights = weights[:n]

        total_weight = sum(weights)
        if total_weight <= 0:
            return float("inf")

        weighted_sum = 0.0
        for metrics, w in zip(scenario_results, weights):
            score, _ = self._compute_score(metrics)
            weighted_sum += score * w

        return weighted_sum / total_weight

    @staticmethod
    def _aggregate_weighted_metrics(
        scenario_results: List[Dict[str, float]],
        weights: List[float],
    ) -> Dict[str, float]:
        """按权重聚合多场景指标。

        Parameters
        ----------
        scenario_results : list[dict]
            每个场景的指标字典
        weights : list[float]
            每个场景的权重

        Returns
        -------
        dict
            加权平均指标
        """
        if not scenario_results:
            return {}

        total_weight = sum(weights)
        if total_weight <= 0:
            return scenario_results[0] if scenario_results else {}

        # 收集所有指标键
        keys: set = set()
        for m in scenario_results:
            keys.update(m.keys())

        aggregated: Dict[str, float] = {}
        for key in keys:
            if key.startswith("_"):
                continue  # 跳过内部标记
            weighted_sum = 0.0
            for metrics, w in zip(scenario_results, weights):
                if key in metrics:
                    weighted_sum += metrics[key] * w
            aggregated[key] = weighted_sum / total_weight

        return aggregated


# ==========================================================================
# SealedSUMOEvaluator — Deployable Champion 的必需评估器
# ==========================================================================

class SealedSUMOEvaluator:
    """Sealed SUMO 评估器 — 用于 deployable champion 的必需条件。

    与 SUMOEvaluator 的区别：
    - 做 paired evaluation：candidate vs incumbent
    - 使用完全相同的 route seed、demand seed、scenario hash
    - 先跑 incumbent 基线，再跑 candidate
    - 返回结构化的 PairedEvalReport（含 delta 指标）
    - 支持多 seed 重复评估
    - 严格非退化门槛检查

    使用方式：
        evaluator = SealedSUMOEvaluator(
            sumocfg_path="...",
            neighbor_graph=...,
            constraints=...,
        )
        report = evaluator.paired_evaluate(
            incumbent_code=incumbent_code,
            candidate_code=candidate_code,
            skill_type="cycle",
            crossing_id="123",
            cohort=cohort,
        )
        if report.passed:
            # candidate 可以成为 deployable champion
    """

    def __init__(
        self,
        sumocfg_path: str,
        neighbor_graph: NeighborGraph,
        constraints: NetworkConstraints,
        eval_duration: float = 600.0,
        n_seeds: int = 3,
        step_length: float = 1.0,
        decision_interval: float = 5.0,
        warmup_steps: int = 100,
    ):
        """
        Parameters 与 SUMOEvaluator 基本一致，但没有 thresholds 参数
        （使用固定的非退化门槛 NON_DEGRADATION_GATES）。
        """
        # 内部持有 SUMOEvaluator 实例来复用仿真逻辑
        self._inner = SUMOEvaluator(
            sumocfg_path=sumocfg_path,
            neighbor_graph=neighbor_graph,
            constraints=constraints,
            eval_duration=eval_duration,
            n_seeds=n_seeds,
            step_length=step_length,
            decision_interval=decision_interval,
            warmup_steps=warmup_steps,
        )
        self.n_seeds = n_seeds

    @property
    def sumocfg_path(self) -> str:
        return self._inner.sumocfg_path

    @sumocfg_path.setter
    def sumocfg_path(self, value: str) -> None:
        self._inner.sumocfg_path = value

    # ==================================================================
    # Public API
    # ==================================================================

    def paired_evaluate(
        self,
        incumbent_code: str,
        candidate_code: str,
        skill_type: str,
        crossing_id: str,
        cohort: SkillCohort,
        seeds: Optional[List[int]] = None,
    ) -> PairedEvalReport:
        """执行 sealed paired evaluation。

        流程：
        1. 生成一组固定的 seeds
        2. 用这些 seeds 先评估 incumbent
        3. 用完全相同的 seeds 评估 candidate
        4. 比较指标，判定是否通过非退化门槛

        Parameters
        ----------
        incumbent_code : str
            当前 incumbent（seed 或上一代 champion）的代码
        candidate_code : str
            候选代码
        skill_type : str
            "cycle" 或 "phase"
        crossing_id : str
            目标路口 ID
        cohort : SkillCohort
            当前 skill 集合（其他路口保持不变）
        seeds : list[int], optional
            自定义种子列表；如果不提供则自动生成 n_seeds 个

        Returns
        -------
        PairedEvalReport
            结构化的对比报告
        """
        if seeds is None:
            seeds = [42 + i * 7 for i in range(self.n_seeds)]

        candidate_id = (
            f"paired_{crossing_id}_{skill_type}_{uuid.uuid4().hex[:8]}"
        )
        incumbent_id = (
            f"incumbent_{crossing_id}_{skill_type}"
        )

        logger.info(
            "Sealed paired evaluation 开始: crossing=%s type=%s n_seeds=%d",
            crossing_id, skill_type, len(seeds),
        )

        # ---- Step 1: 评估 incumbent（基线） ----
        incumbent_report = self._inner.evaluate_multi_seed(
            candidate_code=incumbent_code,
            skill_type=skill_type,
            crossing_id=crossing_id,
            cohort=cohort,
            seeds=seeds,
        )
        incumbent_metrics = incumbent_report.metrics

        if incumbent_report.score == float("inf"):
            # incumbent 评估失败（SUMO 不可用等）
            return PairedEvalReport(
                candidate_id=candidate_id,
                incumbent_id=incumbent_id,
                crossing_id=crossing_id,
                skill_type=skill_type,
                incumbent_metrics={},
                candidate_metrics={},
                seeds_used=seeds,
                n_seeds=len(seeds),
                passed=False,
                rejection_reason=f"Incumbent SUMO 评估失败: "
                                 f"{'; '.join(incumbent_report.violations[:3])}",
                incumbent_report=incumbent_report.to_dict(),
            )

        logger.info(
            "Incumbent 评估完成: score=%.4f, mean_waiting=%.2f, "
            "mean_queue=%.2f, throughput=%.1f",
            incumbent_report.score,
            incumbent_metrics.get("mean_waiting", 0.0),
            incumbent_metrics.get("mean_queue", 0.0),
            incumbent_metrics.get("throughput", 0.0),
        )

        # ---- Step 2: 评估 candidate（使用完全相同的 seeds） ----
        candidate_report = self._inner.evaluate_multi_seed(
            candidate_code=candidate_code,
            skill_type=skill_type,
            crossing_id=crossing_id,
            cohort=cohort,
            seeds=seeds,
        )
        candidate_metrics = candidate_report.metrics

        if candidate_report.score == float("inf"):
            return PairedEvalReport(
                candidate_id=candidate_id,
                incumbent_id=incumbent_id,
                crossing_id=crossing_id,
                skill_type=skill_type,
                incumbent_metrics=incumbent_metrics,
                candidate_metrics={},
                seeds_used=seeds,
                n_seeds=len(seeds),
                passed=False,
                rejection_reason=f"Candidate SUMO 评估失败: "
                                 f"{'; '.join(candidate_report.violations[:3])}",
                incumbent_report=incumbent_report.to_dict(),
                candidate_report=candidate_report.to_dict(),
            )

        logger.info(
            "Candidate 评估完成: score=%.4f, mean_waiting=%.2f, "
            "mean_queue=%.2f, throughput=%.1f",
            candidate_report.score,
            candidate_metrics.get("mean_waiting", 0.0),
            candidate_metrics.get("mean_queue", 0.0),
            candidate_metrics.get("throughput", 0.0),
        )

        # ---- Step 3: 计算 delta ----
        delta = self._compute_delta(incumbent_metrics, candidate_metrics)

        # ---- Step 4: 非退化门槛检查 ----
        passed, rejection_reason, gate_details = self._check_non_degradation_gates(
            incumbent_metrics, candidate_metrics,
        )

        report = PairedEvalReport(
            candidate_id=candidate_id,
            incumbent_id=incumbent_id,
            crossing_id=crossing_id,
            skill_type=skill_type,
            incumbent_metrics=incumbent_metrics,
            candidate_metrics=candidate_metrics,
            delta=delta,
            seeds_used=seeds,
            n_seeds=len(seeds),
            passed=passed,
            rejection_reason=rejection_reason,
            gate_details=gate_details,
            incumbent_report=incumbent_report.to_dict(),
            candidate_report=candidate_report.to_dict(),
        )

        if passed:
            logger.info(
                "Sealed paired evaluation PASSED: %s vs %s",
                candidate_id, incumbent_id,
            )
        else:
            logger.info(
                "Sealed paired evaluation FAILED: %s vs %s — %s",
                candidate_id, incumbent_id, rejection_reason,
            )

        return report

    def evaluate_candidate(
        self,
        candidate_code: str,
        skill_type: str,
        crossing_id: str,
        cohort: SkillCohort,
        seed: int = 42,
    ) -> SUMOEvalReport:
        """向后兼容的 evaluate_candidate 接口 — 委托给内部 SUMOEvaluator。"""
        return self._inner.evaluate_candidate(
            candidate_code=candidate_code,
            skill_type=skill_type,
            crossing_id=crossing_id,
            cohort=cohort,
            seed=seed,
        )

    def evaluate_multi_seed(
        self,
        candidate_code: str,
        skill_type: str,
        crossing_id: str,
        cohort: SkillCohort,
        seeds: Optional[List[int]] = None,
    ) -> SUMOEvalReport:
        """向后兼容的 evaluate_multi_seed 接口 — 委托给内部 SUMOEvaluator。"""
        return self._inner.evaluate_multi_seed(
            candidate_code=candidate_code,
            skill_type=skill_type,
            crossing_id=crossing_id,
            cohort=cohort,
            seeds=seeds,
        )

    def evaluate_multi_scenario(
        self,
        candidate_code: str,
        skill_type: str,
        crossing_id: str,
        cohort: SkillCohort,
        scenario_catalog: ScenarioCatalog,
        n_seeds: int = 2,
    ) -> SUMOEvalReport:
        """向后兼容的 evaluate_multi_scenario 接口 — 委托给内部 SUMOEvaluator。"""
        return self._inner.evaluate_multi_scenario(
            candidate_code=candidate_code,
            skill_type=skill_type,
            crossing_id=crossing_id,
            cohort=cohort,
            scenario_catalog=scenario_catalog,
            n_seeds=n_seeds,
        )

    # ==================================================================
    # Delta & Gate computation
    # ==================================================================

    @staticmethod
    def _compute_delta(
        incumbent_metrics: Dict[str, float],
        candidate_metrics: Dict[str, float],
    ) -> Dict[str, float]:
        """计算 candidate 相对 incumbent 的 delta（candidate - incumbent）。

        负值表示 candidate 更好（如 waiting_time 更低），正值表示更差。
        throughput 的 delta 是正值表示更好。
        """
        delta: Dict[str, float] = {}

        # 标准指标 delta
        standard_keys = [
            "mean_waiting", "mean_queue", "max_queue",
            "throughput", "avg_throughput_per_step",
            "safety_overrides", "safety_override_ratio",
            "phase_starvation_count", "phase_starvation_ratio",
            "spillback_ratio", "spillback_events",
        ]

        for key in standard_keys:
            inc_val = incumbent_metrics.get(key, 0.0)
            cand_val = candidate_metrics.get(key, 0.0)
            delta[key] = round(cand_val - inc_val, 6)

        # 归一化 delta（相对变化率）
        for key in standard_keys:
            inc_val = incumbent_metrics.get(key, 0.0)
            if abs(inc_val) > 1e-9:
                delta[f"{key}_ratio"] = round(
                    (candidate_metrics.get(key, 0.0) - inc_val) / abs(inc_val),
                    6,
                )
            else:
                delta[f"{key}_ratio"] = 0.0

        return delta

    @staticmethod
    def _check_non_degradation_gates(
        incumbent_metrics: Dict[str, float],
        candidate_metrics: Dict[str, float],
    ) -> Tuple[bool, str, Dict[str, dict]]:
        """检查所有非退化门槛。

        Returns
        -------
        (passed, rejection_reason, gate_details)
            passed: True 表示通过所有门槛
            rejection_reason: 第一个失败的门槛的原因（空字符串表示全部通过）
            gate_details: 每个门槛的详细检查结果
        """
        gate_details: Dict[str, dict] = {}
        all_passed = True
        first_failure = ""

        for gate_name, gate_spec in NON_DEGRADATION_GATES.items():
            metric_key = gate_spec["metric_key"]
            direction = gate_spec["direction"]
            description = gate_spec["description"]

            inc_val = incumbent_metrics.get(metric_key, 0.0)
            cand_val = candidate_metrics.get(metric_key, 0.0)

            detail: Dict[str, Any] = {
                "gate": gate_name,
                "description": description,
                "incumbent_value": inc_val,
                "candidate_value": cand_val,
                "passed": True,
            }

            if direction == "lower_bound":
                # candidate 不能低于 incumbent * threshold_ratio
                threshold_ratio = gate_spec["threshold_ratio"]
                lower_bound = inc_val * threshold_ratio
                gate_passed = cand_val >= lower_bound
                detail["threshold"] = lower_bound
                detail["threshold_ratio"] = threshold_ratio
                if not gate_passed:
                    detail["reason"] = (
                        f"{metric_key}={cand_val:.2f} < "
                        f"incumbent*{threshold_ratio}={lower_bound:.2f}"
                    )

            elif direction == "upper_bound":
                # candidate 不能高于 incumbent * threshold_ratio
                threshold_ratio = gate_spec["threshold_ratio"]
                upper_bound = inc_val * threshold_ratio
                # 如果 incumbent 为 0，使用绝对容差
                if abs(inc_val) < 1e-9:
                    gate_passed = cand_val <= 0.0
                    detail["threshold"] = 0.0
                else:
                    gate_passed = cand_val <= upper_bound
                    detail["threshold"] = upper_bound
                detail["threshold_ratio"] = threshold_ratio
                if not gate_passed:
                    detail["reason"] = (
                        f"{metric_key}={cand_val:.2f} > "
                        f"incumbent*{threshold_ratio}={upper_bound:.2f}"
                    )

            elif direction == "absolute_zero":
                # candidate 的值必须为 0
                gate_passed = cand_val <= 0.0
                detail["threshold"] = 0.0
                if not gate_passed:
                    detail["reason"] = (
                        f"{metric_key}={cand_val:.2f} > 0"
                    )

            else:
                gate_passed = True
                detail["reason"] = f"未知方向: {direction}"

            detail["passed"] = gate_passed
            gate_details[gate_name] = detail

            if not gate_passed:
                all_passed = False
                if not first_failure:
                    first_failure = (
                        f"{description}: {detail.get('reason', '未通过')}"
                    )

        return all_passed, first_failure, gate_details
