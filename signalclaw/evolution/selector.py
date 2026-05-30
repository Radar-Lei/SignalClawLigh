"""SkillSelector - 多目标选择器。

从候选 Skill 中选择最优者，基于多目标加权评分。

分层选择策略：
- archive candidate：AST + replay 通过即可入选档案
- champion candidate：必须有真实 sumo_report，必须在 sealed scenario 上
  与 seed baseline 比较且满足所有硬门槛
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from signalclaw.evolution.archive import ArchiveEntry

logger = logging.getLogger(__name__)


class SkillSelector:
    """多目标选择器：基于加权评分从候选中选择最佳 Skill。

    两级过滤：
    1. archive 级：AST 通过 + replay 通过 → 可进入档案
    2. champion 级：真实 SUMO 评估 + 所有硬门槛 → 可成为 champion
    """

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        sumo_evaluator=None,
    ):
        # 权重：正值表示越小越好，负值表示越大越好
        self.weights = weights or {
            "mean_waiting": 1.0,
            "mean_queue": 1.0,
            "travel_time": 0.6,
            "throughput": -0.6,  # 负号表示越高越好
            "safety_violation": 2.0,
            "spillback": 1.5,
            "phase_starvation": 1.0,
            "cycle_volatility": 0.5,
            "neighbor_damage": 0.5,
            "code_complexity": 0.05,
        }

        # SUMO 评估器引用（用于按需触发 seed baseline 评估）
        self._sumo_evaluator = sumo_evaluator

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(
        self,
        candidates: List[ArchiveEntry],
        crossing_id: str,
        skill_type: str,
        seed_entry: Optional[ArchiveEntry] = None,
    ) -> Optional[ArchiveEntry]:
        """从候选中选择最佳。

        优先选 champion（必须通过 SUMO 硬门槛），
        如果没有 champion 则退回 archive 级候选。

        Parameters
        ----------
        candidates : List[ArchiveEntry]
            候选列表
        crossing_id : str
            路口 ID
        skill_type : str
            "cycle" 或 "phase"
        seed_entry : ArchiveEntry, optional
            seed skill 的 ArchiveEntry，用于 SUMO baseline 比较。
            如果不提供，则无法进行 champion 级比较。

        Returns
        -------
        ArchiveEntry or None
        """
        # 第一级：archive 过滤（AST + replay）
        archive_valid = self._filter_archive_candidates(candidates)
        if not archive_valid:
            return None

        # 尝试 champion 级选择
        seed_baseline = self._get_seed_baseline(seed_entry)
        champion_valid = self._filter_champions(archive_valid, seed_baseline)

        if champion_valid:
            # 从 champion 中按软目标 Pareto 排名选最优
            scored = [(c, self.compute_objective(c, seed_baseline)) for c in champion_valid]
            scored.sort(key=lambda x: x[1])
            logger.info(
                "Champion 级选择: %d 个候选通过硬门槛，最优 score=%.4f",
                len(champion_valid), scored[0][1],
            )
            return scored[0][0]

        # 退回 archive 级选择
        logger.info(
            "无候选通过 champion 硬门槛，退回 archive 级选择 (%d 候选)",
            len(archive_valid),
        )
        scored = [(c, self.compute_objective(c, seed_baseline)) for c in archive_valid]
        scored.sort(key=lambda x: x[1])
        return scored[0][0]

    def compute_objective(
        self,
        entry: ArchiveEntry,
        seed_baseline: Optional[Dict[str, float]] = None,
    ) -> float:
        """计算多目标加权分数（越低越好）。

        软目标 Pareto 排名维度：
        - avg_travel_time 越低越好
        - avg_waiting_time 越低越好
        - avg_queue 越低越好
        - throughput/completed 越高越好
        - downstream spillback 越低越好
        - cycle volatility 越低越好
        - code complexity 越低越好
        """
        score = 0.0

        # 1. Replay score（0-1，越高越好 → 取负数使其越低越好）
        replay_score = 0.0
        if entry.replay_report:
            replay_score = entry.replay_report.get("score", 0.0)
        score -= replay_score * 10.0

        # 2. 代码复杂度（越低越好）
        complexity = 0.0
        if entry.static_check:
            complexity = entry.static_check.get("complexity_score", 0.0)
        score += complexity * self.weights.get("code_complexity", 0.05)

        # 3. Safety violation 惩罚
        n_violations = 0
        if entry.replay_report:
            violations = entry.replay_report.get("violations", [])
            n_violations = len(violations)
        score += n_violations * self.weights.get("safety_violation", 2.0)

        # 4. Phase starvation 惩罚
        starvation_count = 0
        if entry.replay_report:
            failure_cases = entry.replay_report.get("failure_cases", [])
            starvation_count = sum(
                1 for fc in failure_cases
                if isinstance(fc, dict) and "starvation" in fc.get("violation", "")
            )
        score += starvation_count * self.weights.get("phase_starvation", 1.0)

        # 5. Test coverage bonus
        if entry.replay_report:
            test_cases = entry.replay_report.get("test_cases_run", 0)
            passed = entry.replay_report.get("passed", False)
            if passed and test_cases > 0:
                score -= test_cases * 0.1

        # 6. SUMO 仿真软目标评分（如果有真实报告）
        if entry.sumo_report and self._is_real_sumo_report(entry):
            metrics = entry.sumo_report.get("metrics", {})

            # avg_waiting_time 越低越好
            avg_wait = metrics.get("mean_waiting", 0.0)
            score += avg_wait * self.weights.get("mean_waiting", 1.0)

            # avg_queue 越低越好
            avg_queue = metrics.get("mean_queue", 0.0)
            score += avg_queue * self.weights.get("mean_queue", 1.0)

            # throughput 越高越好（取负数）
            throughput = metrics.get("throughput", 0.0)
            score -= throughput * abs(self.weights.get("throughput", -0.6))

            # downstream spillback 越低越好
            spillback = metrics.get("spillback_ratio", 0.0)
            score += spillback * self.weights.get("spillback", 1.5) * 10.0

            # phase starvation 越低越好
            p_starv = metrics.get("phase_starvation_ratio", 0.0)
            score += p_starv * self.weights.get("phase_starvation", 1.0) * 10.0

            # safety override 越低越好
            safety_ratio = metrics.get("safety_override_ratio", 0.0)
            score += safety_ratio * self.weights.get("safety_violation", 2.0) * 20.0

            # SUMO 通过 bonus
            if entry.sumo_report.get("passed", False):
                score -= 5.0

            # 相对于 seed baseline 的改善 bonus
            if seed_baseline:
                score += self._compute_relative_score(metrics, seed_baseline)

        # 7. Generation penalty（轻微倾向于早期发现的优秀候选）
        score += entry.generation * 0.01

        return score

    def rank(
        self,
        candidates: List[ArchiveEntry],
        seed_entry: Optional[ArchiveEntry] = None,
    ) -> List[tuple]:
        """对所有候选排序并返回 (entry, score) 列表。"""
        seed_baseline = self._get_seed_baseline(seed_entry)
        archive_valid = self._filter_archive_candidates(candidates)
        scored = [(c, self.compute_objective(c, seed_baseline)) for c in archive_valid]
        scored.sort(key=lambda x: x[1])
        return scored

    # ------------------------------------------------------------------
    # Seed baseline
    # ------------------------------------------------------------------

    def _get_seed_baseline(
        self, seed_entry: Optional[ArchiveEntry]
    ) -> Optional[Dict[str, float]]:
        """获取 seed skill 的 SUMO baseline 指标。

        如果 seed 有真实的 sumo_report，直接提取 metrics。
        如果 seed 没有 sumo_report 但有 sumo_evaluator，触发一次评估。
        如果两者都没有，返回 None（champion 硬门槛将无法通过）。
        """
        if seed_entry is None:
            return None

        # seed 已有真实 SUMO 报告 → 直接用
        if seed_entry.sumo_report and self._is_real_sumo_report(seed_entry):
            return seed_entry.sumo_report.get("metrics", {})

        # 尝试触发 SUMO 评估获取 baseline
        if self._sumo_evaluator is not None and seed_entry.code:
            logger.info("Seed skill 无 SUMO 报告，尝试触发 baseline 评估...")
            try:
                from signalclaw.skills.cohort import SkillCohort
                # 注意：这里需要外部调用者确保 evaluator 有正确的上下文
                # 如果 evaluator 不可用或上下文不完整，返回 None
                report = self._sumo_evaluator.evaluate_candidate(
                    candidate_code=seed_entry.code,
                    skill_type=seed_entry.skill_type,
                    crossing_id=seed_entry.crossing_id,
                    cohort=None,  # 需要 cohort，但此处可能不可用
                )
                if report and report.metrics:
                    # 将报告写回 seed entry
                    seed_entry.set_sumo_report(report)
                    return report.metrics
            except Exception as e:
                logger.warning("无法触发 seed SUMO baseline 评估: %s", e)

        return None

    # ------------------------------------------------------------------
    # Champion hard gates
    # ------------------------------------------------------------------

    def _filter_champions(
        self,
        candidates: List[ArchiveEntry],
        seed_baseline: Optional[Dict[str, float]],
    ) -> List[ArchiveEntry]:
        """Champion 硬门槛过滤。

        任一条件不满足即淘汰：

        硬门槛清单：
        - AST 必须通过
        - unit tests 必须通过（replay_report.passed = True）
        - safety violations = 0
        - phase starvation = 0
        - sumo_score 必须是真实评估值（不允许 0 或空值）
        - completed_vehicles 不得比 seed 下降超过 1%
        - avg_waiting_time 不得比 seed 上升超过 3%
        - avg_queue 不得比 seed 上升超过 3%
        - safety_clip_count 不得显著增加
        """
        champions = []
        for c in candidates:
            reason = self._check_champion_gates(c, seed_baseline)
            if reason is None:
                champions.append(c)
            else:
                logger.debug(
                    "候选 %s 未通过 champion 硬门槛: %s",
                    c.candidate_id, reason,
                )
        return champions

    def _check_champion_gates(
        self,
        entry: ArchiveEntry,
        seed_baseline: Optional[Dict[str, float]],
    ) -> Optional[str]:
        """检查单个候选是否满足所有 champion 硬门槛。

        Returns
        -------
        None 如果通过所有门槛，否则返回拒绝原因。
        """
        # ── 1. AST 必须通过 ──
        if not entry.static_check or not entry.static_check.get("passed", False):
            return "AST 检查未通过"

        # ── 2. Unit tests 必须通过 ──
        if not entry.replay_report or not entry.replay_report.get("passed", False):
            return "Replay/unit-test 检查未通过"

        # ── 3. Safety violations = 0 ──
        violations = entry.replay_report.get("violations", [])
        if violations:
            return f"存在 {len(violations)} 条 safety violation"

        # ── 4. Phase starvation = 0 ──
        failure_cases = entry.replay_report.get("failure_cases", [])
        starvation_count = sum(
            1 for fc in failure_cases
            if isinstance(fc, dict) and "starvation" in fc.get("violation", "")
        )
        if starvation_count > 0:
            return f"存在 {starvation_count} 次 phase starvation"

        # ── 5. 必须有真实 SUMO 报告 ──
        if not entry.sumo_report:
            return "缺少 SUMO 评估报告"
        if not self._is_real_sumo_report(entry):
            return "SUMO 评估报告不是真实评估值（score=0 或无 metrics）"

        metrics = entry.sumo_report.get("metrics", {})
        if not metrics:
            return "SUMO 报告中无 metrics 数据"

        # ── 6-8. 与 seed baseline 的回归比较 ──
        if seed_baseline:
            # 6. completed_vehicles (throughput) 不得比 seed 下降超过 1%
            seed_throughput = seed_baseline.get("throughput", 0.0)
            cand_throughput = metrics.get("throughput", 0.0)
            if seed_throughput > 0 and cand_throughput < seed_throughput * 0.99:
                return (
                    f"吞吐量下降超过 1%: "
                    f"seed={seed_throughput:.1f}, candidate={cand_throughput:.1f}"
                )

            # 7. avg_waiting_time 不得比 seed 上升超过 3%
            seed_wait = seed_baseline.get("mean_waiting", 0.0)
            cand_wait = metrics.get("mean_waiting", 0.0)
            if seed_wait > 0 and cand_wait > seed_wait * 1.03:
                return (
                    f"平均等待时间上升超过 3%: "
                    f"seed={seed_wait:.2f}, candidate={cand_wait:.2f}"
                )

            # 8. avg_queue 不得比 seed 上升超过 3%
            seed_queue = seed_baseline.get("mean_queue", 0.0)
            cand_queue = metrics.get("mean_queue", 0.0)
            if seed_queue > 0 and cand_queue > seed_queue * 1.03:
                return (
                    f"平均排队长度上升超过 3%: "
                    f"seed={seed_queue:.2f}, candidate={cand_queue:.2f}"
                )

            # 9. safety_clip_count 不得显著增加（超过 seed 的 120%）
            seed_safety = seed_baseline.get("safety_overrides", 0.0)
            cand_safety = metrics.get("safety_overrides", 0.0)
            if seed_safety > 0 and cand_safety > seed_safety * 1.2:
                return (
                    f"safety clip 显著增加: "
                    f"seed={seed_safety:.0f}, candidate={cand_safety:.0f}"
                )
        else:
            # 无 seed baseline 时，不允许通过 champion 门槛
            return "缺少 seed baseline，无法进行回归比较"

        return None

    # ------------------------------------------------------------------
    # Archive-level filtering
    # ------------------------------------------------------------------

    def _filter_archive_candidates(
        self, candidates: List[ArchiveEntry]
    ) -> List[ArchiveEntry]:
        """Archive 级过滤：AST + replay 通过即可。

        这是最低门槛，仅用于保证基本质量。
        """
        valid = []
        for c in candidates:
            # 必须有代码
            if not c.code:
                continue

            # 必须通过 AST 检查
            if c.static_check and not c.static_check.get("passed", False):
                continue

            # 必须通过 Replay 评估
            if c.replay_report and not c.replay_report.get("passed", False):
                continue

            # 代码复杂度不超过阈值
            if c.static_check:
                complexity = c.static_check.get("complexity_score", 0.0)
                if complexity > 50.0:
                    continue

            valid.append(c)

        return valid

    # 保持旧接口名兼容
    _filter_candidates = _filter_archive_candidates

    # ------------------------------------------------------------------
    # SUMO report helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_real_sumo_report(entry: ArchiveEntry) -> bool:
        """判断 sumo_report 是否为真实评估结果。

        真实评估的标志：
        - sumo_report 存在
        - score 不为 0（0 表示未评估或占位值）
        - metrics 非空（至少有实际仿真数据）
        """
        report = entry.sumo_report
        if report is None:
            return False

        score = report.get("score", 0.0)
        # score=0.0 且 metrics 为空 → 占位报告
        if score == 0.0 and not report.get("metrics"):
            return False

        # score 为 inf → 评估失败的报告
        if score == float("inf"):
            return False

        # 有 metrics 且 score 不为 0 → 真实报告
        if report.get("metrics"):
            return True

        # score 非 0 但无 metrics → 可能是简化报告，仍算真实
        return score != 0.0

    @staticmethod
    def _compute_relative_score(
        metrics: Dict[str, float],
        seed_baseline: Dict[str, float],
    ) -> float:
        """计算候选相对于 seed baseline 的相对改善分（负数=改善）。

        用于 Pareto 排名中的相对比较维度。
        """
        rel_score = 0.0

        # avg_waiting_time 改善
        seed_wait = seed_baseline.get("mean_waiting", 0.0)
        cand_wait = metrics.get("mean_waiting", 0.0)
        if seed_wait > 0:
            rel_score += (cand_wait - seed_wait) / seed_wait * 3.0

        # avg_queue 改善
        seed_queue = seed_baseline.get("mean_queue", 0.0)
        cand_queue = metrics.get("mean_queue", 0.0)
        if seed_queue > 0:
            rel_score += (cand_queue - seed_queue) / seed_queue * 2.0

        # throughput 改善（越高越好，取负数）
        seed_tp = seed_baseline.get("throughput", 0.0)
        cand_tp = metrics.get("throughput", 0.0)
        if seed_tp > 0:
            rel_score -= (cand_tp - seed_tp) / seed_tp * 2.0

        # spillback 改善
        seed_spill = seed_baseline.get("spillback_ratio", 0.0)
        cand_spill = metrics.get("spillback_ratio", 0.0)
        if seed_spill > 0:
            rel_score += (cand_spill - seed_spill) / seed_spill * 1.5

        return rel_score
