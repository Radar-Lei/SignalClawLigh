"""CohortSearch - 全网 cohort 组合搜索。

单路口 champion 通过 SealedTournament 后，做全网 cohort 组合搜索，
验证 cohort 组合在全网尺度上不退化。

核心问题：某个路口局部变好可能把拥堵推给下游。

搜索策略：
- 贪心搜索（默认）: 按单路口 improvement 从高到低排序，逐个替换并验证
- Beam search: 维护 beam_width 个候选 cohort，每步扩展后保留最优的

每次替换后运行全网 SUMO 仿真，检查：
- 全网 completed_vehicles 不退化
- 全网 avg_queue 不退化
- 全网 avg_waiting_time 不退化

使用方式::

    from signalclaw.evolution.cohort_search import CohortSearch, CohortSearchConfig

    config = CohortSearchConfig(beam_width=3)
    search = CohortSearch(
        config=config,
        tournament_results=tournament_results,
        seed_cohort=seed_cohort,
        evolved_cohort=evolved_cohort,
        sumocfg_path="sumo_scenarios/chengdu/chengdu.sumocfg",
        neighbor_graph_path="artifacts/topology/one_hop_neighbors.json",
    )
    result = search.search()
    result.save("artifacts/evolution_archive/cohort_search_result.json")
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from signalclaw.evolution.tournament import TournamentResult
from signalclaw.skills.cohort import SkillCohort

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CohortSearchConfig:
    """全网 cohort 组合搜索配置。"""

    max_cohort_size: int = 10          # 最大 cohort 规模（路口数，0=不限制）
    beam_width: int = 3                # beam search 宽度
    network_sim_duration: float = 3600.0  # 全网仿真时长
    degradation_threshold: float = 0.02   # 允许退化的比例（2%）
    seed: int = 42                     # 随机种子
    strategy: str = "greedy"           # "greedy" or "beam"
    # 全网评估指标权重（用于 beam search 综合打分，越小越好）
    metric_weights: Dict[str, float] = field(default_factory=lambda: {
        "avg_queue": 1.0,
        "avg_waiting_time": 1.0,
        "completed_vehicles": -0.5,  # 负号表示越大越好
    })


@dataclass
class CohortCandidate:
    """单个路口的候选组合信息。"""

    crossing_id: str
    champion_skill_id: str       # champion 的 candidate_id
    champion_score: Optional[float] = None
    seed_skill_id: str = ""
    seed_score: Optional[float] = None
    improvement_pct: float = 0.0  # 单路口改善百分比

    # champion skill 的目录路径（cycle 和 phase）
    cycle_dir: str = ""
    phase_dir: str = ""
    # seed skill 的目录路径（用于回退）
    seed_cycle_dir: str = ""
    seed_phase_dir: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CohortSearchResult:
    """全网 cohort 组合搜索结果。"""

    final_cohort: Dict[str, str]         # crossing_id -> "champion" | "seed"
    network_metrics: Dict[str, float]    # 最终全网指标
    baseline_metrics: Dict[str, float]   # 基线指标（全 seed）
    network_improvement_pct: float       # 全网改善百分比
    accepted_count: int                  # 接受 champion 的路口数
    reverted_count: int                  # 回退到 seed 的路口数
    search_history: List[Dict]           # 搜索过程记录
    strategy: str = "greedy"
    beam_width: int = 1
    total_evaluations: int = 0           # 总仿真次数

    def to_dict(self) -> dict:
        return {
            "final_cohort": self.final_cohort,
            "network_metrics": self.network_metrics,
            "baseline_metrics": self.baseline_metrics,
            "network_improvement_pct": self.network_improvement_pct,
            "accepted_count": self.accepted_count,
            "reverted_count": self.reverted_count,
            "search_history": self.search_history,
            "strategy": self.strategy,
            "beam_width": self.beam_width,
            "total_evaluations": self.total_evaluations,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def save(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_dict(cls, d: dict) -> "CohortSearchResult":
        return cls(
            final_cohort=d["final_cohort"],
            network_metrics=d["network_metrics"],
            baseline_metrics=d["baseline_metrics"],
            network_improvement_pct=d["network_improvement_pct"],
            accepted_count=d["accepted_count"],
            reverted_count=d["reverted_count"],
            search_history=d.get("search_history", []),
            strategy=d.get("strategy", "greedy"),
            beam_width=d.get("beam_width", 1),
            total_evaluations=d.get("total_evaluations", 0),
        )

    @classmethod
    def load(cls, path: str) -> "CohortSearchResult":
        raw = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(json.loads(raw))


# ---------------------------------------------------------------------------
# CohortSearch
# ---------------------------------------------------------------------------

class CohortSearch:
    """全网 cohort 组合搜索。

    从全 seed baseline 开始，按单路口改善幅度排序，逐步替换 champion 并
    运行全网仿真验证。如果全网指标不退化，接受替换；否则回退。

    Parameters
    ----------
    config : CohortSearchConfig
        搜索配置。
    candidates : List[CohortCandidate]
        每个路口的 champion 候选信息（已按 improvement 排序）。
    seed_cohort : SkillCohort
        seed cohort（全 seed baseline）。
    evolved_cohort : SkillCohort
        进化后 cohort（包含 champion skill 路径）。
    sumocfg_path : str
        SUMO 配置文件路径。
    neighbor_graph_path : str
        邻接图 JSON 路径。
    """

    def __init__(
        self,
        config: CohortSearchConfig,
        candidates: List[CohortCandidate],
        seed_cohort: SkillCohort,
        evolved_cohort: SkillCohort,
        sumocfg_path: str,
        neighbor_graph_path: str,
    ):
        self.config = config
        self.candidates = candidates
        self.seed_cohort = seed_cohort
        self.evolved_cohort = evolved_cohort
        self.sumocfg_path = sumocfg_path
        self.neighbor_graph_path = neighbor_graph_path

        # 按 improvement 从高到低排序
        self.candidates.sort(key=lambda c: c.improvement_pct, reverse=True)

    # ==================================================================
    # Public API
    # ==================================================================

    def search(self) -> CohortSearchResult:
        """执行全网组合搜索。"""
        logger.info(
            "[cohort_search] 开始全网组合搜索: %d 个候选路口, strategy=%s",
            len(self.candidates), self.config.strategy,
        )
        print(f"\n[cohort_search] === 全网组合搜索 ===")
        print(f"[cohort_search] 候选路口数: {len(self.candidates)}")
        print(f"[cohort_search] 策略: {self.config.strategy}")
        print(f"[cohort_search] 退化阈值: {self.config.degradation_threshold}")

        # 1. 评估基线（全 seed）
        print("[cohort_search] 评估基线（全 seed cohort）...")
        baseline_metrics = self._evaluate_cohort(self.seed_cohort)
        print(f"[cohort_search] 基线指标: {self._format_metrics(baseline_metrics)}")

        if not baseline_metrics:
            logger.warning("[cohort_search] 基线评估失败，返回空结果")
            return CohortSearchResult(
                final_cohort={cid: "seed" for cid in self.seed_cohort.skills},
                network_metrics={},
                baseline_metrics={},
                network_improvement_pct=0.0,
                accepted_count=0,
                reverted_count=len(self.candidates),
                search_history=[],
                strategy=self.config.strategy,
                beam_width=self.config.beam_width,
            )

        total_evals = 1  # 基线算 1 次

        # 2. 执行搜索
        if self.config.strategy == "beam":
            final_state, search_history, evals = self._search_beam(baseline_metrics)
        else:
            final_state, search_history, evals = self._search_greedy(baseline_metrics)

        total_evals += evals

        # 3. 构建最终 cohort 并评估
        final_cohort = self._build_cohort_from_state(final_state)
        final_metrics = self._evaluate_cohort(final_cohort)
        total_evals += 1

        # 4. 计算改善
        improvement = self._compute_improvement(baseline_metrics, final_metrics)

        # 5. 统计接受/回退
        accepted = sum(1 for v in final_state.values() if v == "champion")
        reverted = sum(1 for v in final_state.values() if v == "seed")

        result = CohortSearchResult(
            final_cohort=final_state,
            network_metrics=final_metrics,
            baseline_metrics=baseline_metrics,
            network_improvement_pct=improvement,
            accepted_count=accepted,
            reverted_count=reverted,
            search_history=search_history,
            strategy=self.config.strategy,
            beam_width=self.config.beam_width,
            total_evaluations=total_evals,
        )

        print(f"\n[cohort_search] === 搜索完成 ===")
        print(f"[cohort_search] 接受 champion: {accepted}, 回退 seed: {reverted}")
        print(f"[cohort_search] 全网改善: {improvement:+.2f}%")
        print(f"[cohort_search] 基线: {self._format_metrics(baseline_metrics)}")
        print(f"[cohort_search] 最终: {self._format_metrics(final_metrics)}")
        print(f"[cohort_search] 总仿真次数: {total_evals}")

        return result

    # ==================================================================
    # Greedy search
    # ==================================================================

    def _search_greedy(
        self,
        baseline_metrics: Dict[str, float],
    ) -> Tuple[Dict[str, str], List[Dict], int]:
        """贪心搜索：按 improvement 排序，逐个替换并验证。"""
        # state: crossing_id -> "seed" | "champion"
        state = {cid: "seed" for cid in self.seed_cohort.skills}
        search_history = []
        evals = 0

        for i, candidate in enumerate(self.candidates):
            cid = candidate.crossing_id
            print(
                f"\n[cohort_search] 步骤 {i + 1}/{len(self.candidates)}: "
                f"尝试替换 {cid} (improvement={candidate.improvement_pct:+.2f}%)"
            )

            # 尝试替换
            state[cid] = "champion"
            candidate_cohort = self._build_cohort_from_state(state)
            candidate_metrics = self._evaluate_cohort(candidate_cohort)
            evals += 1

            if not candidate_metrics:
                # 评估失败，回退
                state[cid] = "seed"
                search_history.append({
                    "step": i + 1,
                    "crossing_id": cid,
                    "action": "revert",
                    "reason": "evaluation_failed",
                    "improvement_pct": candidate.improvement_pct,
                })
                print(f"  -> 评估失败，回退")
                continue

            # 检查是否退化
            degraded = self._check_degradation(baseline_metrics, candidate_metrics)

            if degraded:
                # 退化，回退
                state[cid] = "seed"
                search_history.append({
                    "step": i + 1,
                    "crossing_id": cid,
                    "action": "revert",
                    "reason": "network_degradation",
                    "improvement_pct": candidate.improvement_pct,
                    "metrics": candidate_metrics,
                })
                print(f"  -> 全网退化，回退")
            else:
                # 接受替换，更新 baseline 为当前最优
                search_history.append({
                    "step": i + 1,
                    "crossing_id": cid,
                    "action": "accept",
                    "improvement_pct": candidate.improvement_pct,
                    "metrics": candidate_metrics,
                })
                # 更新 baseline 以便后续替换在此基础上检查退化
                baseline_metrics = candidate_metrics
                print(f"  -> 接受替换")

        return state, search_history, evals

    # ==================================================================
    # Beam search
    # ==================================================================

    def _search_beam(
        self,
        baseline_metrics: Dict[str, float],
    ) -> Tuple[Dict[str, str], List[Dict], int]:
        """Beam search：维护 beam_width 个候选 cohort，每步扩展后保留最优的。"""
        beam_width = self.config.beam_width
        candidates = self.candidates

        # beam: List of (state, metrics) tuples
        # state: crossing_id -> "seed" | "champion"
        initial_state = {cid: "seed" for cid in self.seed_cohort.skills}
        beam: List[Tuple[Dict[str, str], Dict[str, float]]] = [
            (initial_state, baseline_metrics)
        ]
        search_history = []
        evals = 0

        for i, candidate in enumerate(candidates):
            cid = candidate.crossing_id
            print(
                f"\n[cohort_search] Beam 步骤 {i + 1}/{len(candidates)}: "
                f"扩展 {cid} (improvement={candidate.improvement_pct:+.2f}%)"
            )

            # 扩展每个 beam 中的状态
            expanded: List[Tuple[Dict[str, str], Dict[str, float], str]] = []

            for beam_state, beam_metrics in beam:
                # 选项 1：保持不变
                expanded.append((beam_state, beam_metrics, "keep"))

                # 选项 2：替换为 champion
                new_state = dict(beam_state)
                new_state[cid] = "champion"
                new_cohort = self._build_cohort_from_state(new_state)
                new_metrics = self._evaluate_cohort(new_cohort)
                evals += 1

                if new_metrics and not self._check_degradation(baseline_metrics, new_metrics):
                    expanded.append((new_state, new_metrics, "accept"))
                    search_history.append({
                        "step": i + 1,
                        "crossing_id": cid,
                        "action": "accept",
                        "beam_source": "beam",
                        "improvement_pct": candidate.improvement_pct,
                        "metrics": new_metrics,
                    })
                else:
                    search_history.append({
                        "step": i + 1,
                        "crossing_id": cid,
                        "action": "revert",
                        "reason": "network_degradation_or_eval_failed",
                        "improvement_pct": candidate.improvement_pct,
                    })

            # 按 score 排序，保留 top beam_width
            scored = []
            for state, metrics, action in expanded:
                score = self._compute_network_score(metrics)
                scored.append((score, state, metrics, action))
            scored.sort(key=lambda x: x[0])  # 越小越好

            beam = [(s, m) for _, s, m, _ in scored[:beam_width]]

            best_score = scored[0][0]
            print(f"  -> Beam top score: {best_score:.4f}, beam size: {len(beam)}")

        # 返回最优 beam
        final_state, final_metrics = beam[0]
        return final_state, search_history, evals

    # ==================================================================
    # Cohort evaluation
    # ==================================================================

    def _evaluate_cohort(self, cohort: SkillCohort) -> Dict[str, float]:
        """运行全网仿真评估一组 cohort。

        Returns
        -------
        Dict[str, float]
            全网指标字典，包含 avg_queue, avg_waiting_time, completed_vehicles 等。
            如果评估失败，返回空字典。
        """
        try:
            from signalclaw.experiments.runner import ExperimentRunner

            runner = ExperimentRunner(
                sumocfg_path=self.sumocfg_path,
                seed=self.config.seed,
                sim_duration=self.config.network_sim_duration,
            )

            # 保存临时 cohort 文件
            import tempfile
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="cohort_search_"
            ) as f:
                f.write(cohort.to_json())
                temp_cohort_path = f.name

            try:
                metrics = runner.run_signalclaw_cohort(
                    cohort_path=temp_cohort_path,
                    neighbor_graph_path=self.neighbor_graph_path,
                    method_name="cohort_search",
                    verbose=False,
                )
                summary = metrics.summary()
                return {
                    "completed_vehicles": float(summary.get("completed_vehicles", 0)),
                    "avg_queue": float(summary.get("avg_queue", 0)),
                    "avg_waiting_time": float(summary.get("avg_waiting_time", 0)),
                    "avg_travel_time": float(summary.get("avg_travel_time", 0)),
                    "throughput_per_hour": float(summary.get("throughput_per_hour", 0)),
                    "total_stops": float(summary.get("total_stops", 0) or 0),
                }
            finally:
                try:
                    os.unlink(temp_cohort_path)
                except OSError:
                    pass

        except Exception as e:
            logger.warning("[cohort_search] 全网评估失败: %s", e)
            print(f"  [cohort_search] 评估异常: {e}")
            return {}

    # ==================================================================
    # Cohort construction
    # ==================================================================

    def _build_cohort_from_state(
        self, state: Dict[str, str],
    ) -> SkillCohort:
        """根据 state 字典构建新的 cohort。

        state: crossing_id -> "seed" | "champion"
        - "seed": 使用 seed_cohort 中的 skill 路径
        - "champion": 使用 evolved_cohort 中的 skill 路径
        """
        skills = {}
        for cid, choice in state.items():
            if choice == "champion" and cid in self.evolved_cohort.skills:
                skills[cid] = self.evolved_cohort.skills[cid]
            elif cid in self.seed_cohort.skills:
                skills[cid] = self.seed_cohort.skills[cid]

        champion_count = sum(1 for v in state.values() if v == "champion")
        return SkillCohort(
            cohort_id=f"cohort_search_{champion_count}champs",
            skills=skills,
            frozen=True,
            glm_used_online=False,
            exploration=False,
            created_by="cohort_search",
            source="cohort_search",
        )

    # ==================================================================
    # Degradation check
    # ==================================================================

    def _check_degradation(
        self,
        baseline: Dict[str, float],
        candidate: Dict[str, float],
    ) -> bool:
        """检查 candidate 是否相对于 baseline 有退化。

        退化条件（任一满足即视为退化）：
        - completed_vehicles 下降超过 threshold
        - avg_queue 上升超过 threshold
        - avg_waiting_time 上升超过 threshold
        """
        threshold = self.config.degradation_threshold

        # completed_vehicles: 越大越好
        cv_base = baseline.get("completed_vehicles", 0)
        cv_cand = candidate.get("completed_vehicles", 0)
        if cv_base > 0:
            cv_change = (cv_cand - cv_base) / cv_base
            if cv_change < -threshold:
                logger.info(
                    "[cohort_search] completed_vehicles 退化: %.4f (阈值 %.4f)",
                    cv_change, -threshold,
                )
                return True

        # avg_queue: 越小越好
        aq_base = baseline.get("avg_queue", 0)
        aq_cand = candidate.get("avg_queue", 0)
        if aq_base > 0:
            aq_change = (aq_cand - aq_base) / aq_base
            if aq_change > threshold:
                logger.info(
                    "[cohort_search] avg_queue 退化: %.4f (阈值 %.4f)",
                    aq_change, threshold,
                )
                return True

        # avg_waiting_time: 越小越好
        aw_base = baseline.get("avg_waiting_time", 0)
        aw_cand = candidate.get("avg_waiting_time", 0)
        if aw_base > 0:
            aw_change = (aw_cand - aw_base) / aw_base
            if aw_change > threshold:
                logger.info(
                    "[cohort_search] avg_waiting_time 退化: %.4f (阈值 %.4f)",
                    aw_change, threshold,
                )
                return True

        return False

    # ==================================================================
    # Scoring & formatting
    # ==================================================================

    def _compute_network_score(self, metrics: Dict[str, float]) -> float:
        """计算全网综合分数（用于 beam search 排序，越小越好）。"""
        weights = self.config.metric_weights
        score = 0.0
        for metric_name, weight in weights.items():
            value = metrics.get(metric_name, 0.0)
            score += value * weight
        return score

    @staticmethod
    def _compute_improvement(
        baseline: Dict[str, float],
        final: Dict[str, float],
    ) -> float:
        """计算全网改善百分比（基于综合评分，正值表示改善）。"""
        if not baseline or not final:
            return 0.0

        # 使用 completed_vehicles 作为主要改善指标
        cv_base = baseline.get("completed_vehicles", 0)
        cv_final = final.get("completed_vehicles", 0)
        if cv_base > 0:
            return (cv_final - cv_base) / cv_base * 100.0
        return 0.0

    @staticmethod
    def _format_metrics(metrics: Dict[str, float]) -> str:
        """格式化指标用于打印。"""
        if not metrics:
            return "(空)"
        parts = [
            f"cv={metrics.get('completed_vehicles', 0):.0f}",
            f"queue={metrics.get('avg_queue', 0):.2f}",
            f"wait={metrics.get('avg_waiting_time', 0):.1f}s",
        ]
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------

def build_candidates_from_tournament(
    tournament_stats: Dict[str, Any],
    seed_cohort: SkillCohort,
    evolved_cohort: SkillCohort,
    archive_dir: Optional[str] = None,
) -> List[CohortCandidate]:
    """从 tournament 结果构建 CohortCandidate 列表。

    Parameters
    ----------
    tournament_stats : Dict
        tournament_stats.json 的内容，格式为:
        {crossing_id: {"cycle": {...}, "phase": {...}}}
    seed_cohort : SkillCohort
        seed cohort。
    evolved_cohort : SkillCohort
        进化后 cohort。
    archive_dir : str, optional
        archive 目录路径（用于查找 champion entry）。

    Returns
    -------
    List[CohortCandidate]
        候选列表（未排序）。
    """
    candidates = []

    for crossing_id in evolved_cohort.skills:
        # 检查 evolved cohort 是否与 seed cohort 不同
        evolved_skills = evolved_cohort.skills.get(crossing_id, {})
        seed_skills = seed_cohort.skills.get(crossing_id, {})

        # 如果 evolved 和 seed 相同，说明没有 champion
        if evolved_skills == seed_skills:
            continue

        # 从 tournament_stats 获取 champion 信息
        stats = tournament_stats.get(crossing_id, {})
        cycle_stats = stats.get("cycle", {}) if isinstance(stats, dict) else {}
        phase_stats = stats.get("phase", {}) if isinstance(stats, dict) else {}

        champion_score = None
        # 优先使用 phase champion score，因为 phase 决策更关键
        if phase_stats.get("champion_score") is not None:
            champion_score = phase_stats["champion_score"]
        elif cycle_stats.get("champion_score") is not None:
            champion_score = cycle_stats["champion_score"]

        # 计算 improvement（如果有单路口 SUMO score 可以用）
        improvement = 0.0
        if champion_score is not None and champion_score != 0:
            # 简单估计：score 越低越好，improvement = -score * 10
            improvement = max(0.0, -champion_score * 10.0)

        candidate = CohortCandidate(
            crossing_id=crossing_id,
            champion_skill_id=(
                cycle_stats.get("champion_id", "") or
                phase_stats.get("champion_id", "")
            ),
            champion_score=champion_score,
            seed_skill_id=f"seed_{crossing_id}",
            cycle_dir=evolved_skills.get("cycle", ""),
            phase_dir=evolved_skills.get("phase", ""),
            seed_cycle_dir=seed_skills.get("cycle", ""),
            seed_phase_dir=seed_skills.get("phase", ""),
            improvement_pct=improvement,
        )
        candidates.append(candidate)

    return candidates


def save_champion_cohort(
    result: CohortSearchResult,
    seed_cohort: SkillCohort,
    evolved_cohort: SkillCohort,
    output_path: str,
) -> SkillCohort:
    """根据搜索结果保存最终的 champion cohort。

    Parameters
    ----------
    result : CohortSearchResult
        搜索结果。
    seed_cohort : SkillCohort
        seed cohort（用于回退的路口）。
    evolved_cohort : SkillCohort
        进化后 cohort（用于接受 champion 的路口）。
    output_path : str
        输出文件路径。

    Returns
    -------
    SkillCohort
        最终的 champion cohort。
    """
    skills = {}
    for cid, choice in result.final_cohort.items():
        if choice == "champion" and cid in evolved_cohort.skills:
            skills[cid] = evolved_cohort.skills[cid]
        elif cid in seed_cohort.skills:
            skills[cid] = seed_cohort.skills[cid]

    champion_count = result.accepted_count
    cohort = SkillCohort(
        cohort_id=f"champion_cohort_{champion_count}of{len(result.final_cohort)}",
        skills=skills,
        frozen=True,
        glm_used_online=False,
        exploration=False,
        created_by="cohort_search",
        source="cohort_search_champion",
    )
    cohort.save(output_path)
    logger.info("[cohort_search] Champion cohort 保存到: %s", output_path)
    return cohort
