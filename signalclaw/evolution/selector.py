"""SkillSelector - 多目标选择器。

从候选 Skill 中选择最优者，基于多目标加权评分。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from signalclaw.evolution.archive import ArchiveEntry


class SkillSelector:
    """多目标选择器：基于加权评分从候选中选择最佳 Skill。"""

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(
        self,
        candidates: List[ArchiveEntry],
        crossing_id: str,
        skill_type: str,
    ) -> Optional[ArchiveEntry]:
        """从候选中选择最佳。

        过滤规则：
        1. 必须通过 AST 检查
        2. 必须通过 Replay 评估
        3. 代码复杂度不超过阈值

        选择规则：
        - 按 weighted objective 排序
        - 返回最优

        Parameters
        ----------
        candidates : List[ArchiveEntry]
            候选列表
        crossing_id : str
            路口 ID
        skill_type : str
            "cycle" 或 "phase"

        Returns
        -------
        ArchiveEntry or None
        """
        # 过滤
        valid = self._filter_candidates(candidates)
        if not valid:
            return None

        # 评分
        scored = [(c, self.compute_objective(c)) for c in valid]

        # 排序（越低越好）
        scored.sort(key=lambda x: x[1])

        return scored[0][0]

    def compute_objective(self, entry: ArchiveEntry) -> float:
        """计算多目标加权分数（越低越好）。

        分数组成：
        1. Replay score（越高越好 -> 取负数使其越低越好）
        2. 代码复杂度（越低越好）
        3. Safety violation 惩罚
        4. Phase starvation 惩罚
        5. SUMO 仿真评分（如果有）
        """
        score = 0.0

        # 1. Replay score（0-1，越高越好）
        replay_score = 0.0
        if entry.replay_report:
            replay_score = entry.replay_report.get("score", 0.0)
        # 取负数使其越低越好
        score -= replay_score * 10.0  # 放大权重

        # 2. 代码复杂度
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

        # 5. Test coverage bonus（测试通过越多越好）
        if entry.replay_report:
            test_cases = entry.replay_report.get("test_cases_run", 0)
            passed = entry.replay_report.get("passed", False)
            if passed and test_cases > 0:
                score -= test_cases * 0.1  # bonus

        # 6. SUMO 仿真评分（如果有）
        if entry.sumo_report:
            sumo_score = entry.sumo_report.get("score", 0.0)
            # SUMO 评分越低越好，直接加入（权重较大）
            score += sumo_score * self.weights.get("sumo_eval", 0.5)

            # SUMO 阈值违规惩罚
            sumo_violations = entry.sumo_report.get("violations", [])
            score += len(sumo_violations) * 1.0

            # SUMO 通过 bonus
            if entry.sumo_report.get("passed", False):
                score -= 5.0  # 通过 SUMO 评估的额外 bonus

        # 7. Generation penalty（轻微倾向于早期发现的优秀候选）
        score += entry.generation * 0.01

        return score

    def rank(
        self,
        candidates: List[ArchiveEntry],
    ) -> List[tuple]:
        """对所有候选排序并返回 (entry, score) 列表。"""
        valid = self._filter_candidates(candidates)
        scored = [(c, self.compute_objective(c)) for c in valid]
        scored.sort(key=lambda x: x[1])
        return scored

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _filter_candidates(
        self, candidates: List[ArchiveEntry]
    ) -> List[ArchiveEntry]:
        """过滤掉不符合基本条件的候选。"""
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
