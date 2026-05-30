"""PerIntersectionEvolver - 单路口双 Skill 进化主流程。

进化策略：
  Step 1: 用 seed 作为初始代码
  Step 2: 固定 PhaseSkill，进化 CycleSkill（多轮）
  Step 3: 固定 CycleSkill_best，进化 PhaseSkill（多轮）
  Step 4: 联合修复（可选）
  Step 5: 冻结最佳

每轮进化的三级过滤：
  1. AST 静态检查（快速，无副作用）
  2. Replay 离线安全评估（中等开销，在内存中运行）
  3. SUMO 离线仿真评估（慢，仅在 AST + Replay 都通过时执行）
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from signalclaw.core.constraints import IntersectionConstraints
from signalclaw.evolution.archive import ArchiveEntry, SkillArchive
from signalclaw.evolution.ast_sandbox import ASTSandbox
from signalclaw.evolution.evaluator_replay import ReplayEvaluator
from signalclaw.evolution.evaluator_sumo import SUMOEvaluator, SUMOEvalReport
from signalclaw.evolution.feature_mask import FeatureMask
from signalclaw.evolution.glm_mutator import CandidateSkill, GLMSkillMutator
from signalclaw.evolution.prompt_builder import PromptBuilder
from signalclaw.evolution.selector import SkillSelector
from signalclaw.reference.prior_checker import PriorConsistencyChecker
from signalclaw.reference.profile_schema import SQLReferenceProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Crossing profile builder
# ---------------------------------------------------------------------------

def _build_crossing_profile(
    crossing_id: str,
    constraints: IntersectionConstraints,
    phase_count: int = 4,
) -> str:
    """构造路口拓扑描述（供 GLM prompt 使用）。"""
    return (
        f"路口 ID: {crossing_id}\n"
        f"相位数量: {phase_count}\n"
        f"最小绿灯: {constraints.min_green}s\n"
        f"最大绿灯: {constraints.max_green}s\n"
        f"最小周期: {constraints.min_cycle}s\n"
        f"最大周期: {constraints.max_cycle}s\n"
        f"黄灯时间: {constraints.yellow_time}s\n"
        f"全红时间: {constraints.all_red_time}s\n"
        f"最大延长: {constraints.max_extend}s\n"
        f"最大缩短: {constraints.max_shorten}s\n"
        f"强制相位: {constraints.force_phase_ids or '无'}\n"
    )


# ---------------------------------------------------------------------------
# PerIntersectionEvolver
# ---------------------------------------------------------------------------

class PerIntersectionEvolver:
    """单路口双 Skill 进化器。"""

    def __init__(
        self,
        crossing_id: str,
        glm_mutator: GLMSkillMutator,
        prompt_builder: PromptBuilder,
        ast_sandbox: ASTSandbox,
        replay_evaluator: ReplayEvaluator,
        archive: SkillArchive,
        selector: SkillSelector,
        constraints: IntersectionConstraints,
        phase_count: int = 4,
        sumo_evaluator: Optional[SUMOEvaluator] = None,
        cohort: Optional["SkillCohort"] = None,
        sql_profile: Optional[SQLReferenceProfile] = None,
        feature_mask: Optional[FeatureMask] = None,
    ):
        self.crossing_id = crossing_id
        self.glm_mutator = glm_mutator
        self.prompt_builder = prompt_builder
        self.ast_sandbox = ast_sandbox
        self.replay_evaluator = replay_evaluator
        self.archive = archive
        self.selector = selector
        self.constraints = constraints
        self.phase_count = phase_count
        self.sumo_evaluator = sumo_evaluator
        self.cohort = cohort
        self.sql_profile = sql_profile
        self.feature_mask = feature_mask or FeatureMask()
        self.crossing_profile = _build_crossing_profile(
            crossing_id, constraints, phase_count
        )

        # 如果有 SQL 参考画像，创建 Prior Consistency Checker
        self.prior_checker: Optional[PriorConsistencyChecker] = None
        if sql_profile is not None:
            self.prior_checker = PriorConsistencyChecker(sql_profile)

        # Incumbent 跟踪：用于 select_deployable_champion 的 non-degradation gate
        self._incumbents: Dict[str, Optional[ArchiveEntry]] = {
            "cycle": None,
            "phase": None,
        }

    # ======================================================================
    # Public API
    # ======================================================================

    def evolve(
        self,
        seed_cycle_code: str,
        seed_phase_code: str,
        n_candidates: int = 3,
        max_rounds: int = 2,
    ) -> Dict[str, Optional[ArchiveEntry]]:
        """执行完整的双 Skill 进化流程。

        Parameters
        ----------
        seed_cycle_code : str
            初始 Cycle Skill 代码
        seed_phase_code : str
            初始 Phase Skill 代码
        n_candidates : int
            每轮生成的候选数量
        max_rounds : int
            每种类型的最大进化轮数

        Returns
        -------
        Dict[str, Optional[ArchiveEntry]]
            {"cycle": best_cycle_entry, "phase": best_phase_entry}
        """
        results: Dict[str, Optional[ArchiveEntry]] = {
            "cycle": None,
            "phase": None,
        }

        # ---- Step 1: 注册 seed 到 archive ----
        seed_cycle_entry = self._register_seed(seed_cycle_code, "cycle")
        seed_phase_entry = self._register_seed(seed_phase_code, "phase")

        # Seed 也需要通过基本验证
        seed_cycle_ok = self._validate_and_evaluate(seed_cycle_entry, "cycle")
        seed_phase_ok = self._validate_and_evaluate(seed_phase_entry, "phase")

        if seed_cycle_ok:
            seed_cycle_entry.selected = True
            self.archive.add(seed_cycle_entry)
            self._incumbents["cycle"] = seed_cycle_entry
        if seed_phase_ok:
            seed_phase_entry.selected = True
            self.archive.add(seed_phase_entry)
            self._incumbents["phase"] = seed_phase_entry

        # ---- Step 2: 进化 CycleSkill（固定 PhaseSkill） ----
        current_cycle_code = seed_cycle_code
        current_phase_code = seed_phase_code

        for round_num in range(1, max_rounds + 1):
            best_cycle = self._evolve_cycle(
                current_cycle_code,
                current_phase_code,
                n_candidates=n_candidates,
                round_num=round_num,
            )
            if best_cycle is not None:
                current_cycle_code = best_cycle.code
                results["cycle"] = best_cycle
                self._incumbents["cycle"] = best_cycle
            # 如果没找到更好的，保留当前

        # ---- Step 3: 进化 PhaseSkill（固定 CycleSkill_best） ----
        for round_num in range(1, max_rounds + 1):
            best_phase = self._evolve_phase(
                current_phase_code,
                current_cycle_code,
                n_candidates=n_candidates,
                round_num=round_num,
            )
            if best_phase is not None:
                current_phase_code = best_phase.code
                results["phase"] = best_phase
                self._incumbents["phase"] = best_phase

        # ---- Step 4: 联合修复（可选，用最佳 cycle 重新跑一轮 phase） ----
        if results["cycle"] is not None:
            joint_phase = self._evolve_phase(
                current_phase_code,
                results["cycle"].code,
                n_candidates=max(1, n_candidates - 1),
                round_num=max_rounds + 1,
            )
            if joint_phase is not None:
                results["phase"] = joint_phase
                self._incumbents["phase"] = joint_phase

        # ---- Step 5: 标记最佳 ----
        for skill_type, entry in results.items():
            if entry is not None:
                entry.selected = True
                self.archive.add(entry)

        return results

    # ======================================================================
    # Internal: Cycle Evolution
    # ======================================================================

    def _evolve_cycle(
        self,
        current_code: str,
        paired_phase_code: str,
        n_candidates: int,
        round_num: int,
    ) -> Optional[ArchiveEntry]:
        """一轮 CycleSkill 进化。

        流程：
        1. 构建进化 prompt
        2. 调用 GLM 生成 n_candidates 个候选
        3. AST 检查
        4. Replay 评估
        5. Archive 记录
        6. Selector 选择最佳
        """
        candidates: List[ArchiveEntry] = []

        # 获取历史信息
        archive_summary = self.archive.get_summary(
            self.crossing_id, "cycle"
        )
        failure_cases = self._collect_failure_cases("cycle")

        constraints_str = self.prompt_builder._format_constraints(self.constraints)

        for i in range(n_candidates):
            candidate_id = f"cyc_{self.crossing_id}_r{round_num}_c{i}_{uuid.uuid4().hex[:6]}"

            try:
                # 调用 GLM 生成候选
                candidate: CandidateSkill = self.glm_mutator.mutate_cycle_skill(
                    crossing_profile=self.crossing_profile,
                    parent_skill_code=current_code,
                    failure_cases=failure_cases,
                    constraints=constraints_str,
                    archive_summary=archive_summary,
                )
            except Exception as e:
                # GLM 调用失败，跳过
                entry = ArchiveEntry(
                    candidate_id=candidate_id,
                    crossing_id=self.crossing_id,
                    skill_type="cycle",
                    code="",
                    rejection_reason=f"GLM 调用失败: {e}",
                    generation=round_num,
                )
                self.archive.add(entry)
                continue

            # 构建 prompt 记录
            _, user_prompt = self.prompt_builder.build_cycle_prompt(
                crossing_profile=self.crossing_profile,
                parent_code=current_code,
                failure_cases=failure_cases,
                constraints=constraints_str,
                archive_summary=archive_summary,
            )

            entry = ArchiveEntry(
                candidate_id=candidate_id,
                crossing_id=self.crossing_id,
                skill_type="cycle",
                code=candidate.code,
                prompt=user_prompt[:2000],  # 截断避免过大
                glm_model=getattr(self.glm_mutator.client, "model", "glm"),
                generation=round_num,
            )

            # AST 检查
            ast_result = self.ast_sandbox.check(candidate.code, "cycle")
            entry.set_static_check(ast_result)
            if not ast_result.passed:
                entry.rejection_reason = (
                    f"AST 检查失败: {'; '.join(ast_result.violations[:3])}"
                )
                self.archive.add(entry)
                continue

            # Feature Mask 检查（AST 通过后、Replay 评估之前）
            mask_result = self.feature_mask.check_ast_code(candidate.code)
            if not mask_result.passed:
                violation_msgs = "; ".join(v.message for v in mask_result.violations[:3])
                entry.rejection_reason = f"Feature Mask 违规: {violation_msgs}"
                self.archive.add(entry)
                continue

            # Prior Consistency Check（AST 通过后、Replay 评估之前）
            if self.prior_checker is not None:
                prior_result = self.prior_checker.check(candidate.code, "cycle")
                entry.set_prior_check(prior_result)
                if not prior_result.passed:
                    entry.rejection_reason = (
                        f"先验一致性检查失败: {'; '.join(prior_result.violations[:3])}"
                    )
                    self.archive.add(entry)
                    continue

            # Replay 评估
            replay_report = self.replay_evaluator.evaluate(
                skill_code=candidate.code,
                skill_type="cycle",
                crossing_id=self.crossing_id,
                candidate_id=candidate_id,
            )
            entry.set_replay_report(replay_report)
            if not replay_report.passed:
                entry.rejection_reason = (
                    f"Replay 评估失败: {'; '.join(replay_report.violations[:3])}"
                )
                self.archive.add(entry)
                continue

            # SUMO 离线评估（仅在 AST + Replay 都通过时执行）
            if self.sumo_evaluator is not None and self.cohort is not None:
                sumo_report = self.sumo_evaluator.evaluate_multi_seed(
                    candidate_code=candidate.code,
                    skill_type="cycle",
                    crossing_id=self.crossing_id,
                    cohort=self.cohort,
                )
                entry.set_sumo_report(sumo_report)
                if not sumo_report.passed:
                    entry.rejection_reason = (
                        f"SUMO 评估失败: {'; '.join(sumo_report.violations[:3])}"
                    )
                    self.archive.add(entry)
                    continue

            # 通过所有检查
            candidates.append(entry)
            self.archive.add(entry)

        # 选择最佳
        if not candidates:
            return None

        # archive_best 仅用于日志/分析，不能作为部署候选。
        # archive_best 选取基于 replay/AST 分数，未经过 SUMO sealed eval
        # 和 non-degradation gate 验证，回退到它意味着可能部署一个
        # 实际表现不如 incumbent 的变体，破坏"champion 不能越进化越差"的保证。
        archive_best = self.selector.select_archive_best(
            candidates, seed_entry=None,
        )
        if archive_best is not None:
            logger.info(
                "Cycle 进化 archive_best=%s (仅分析用，不参与部署)",
                archive_best.candidate_id,
            )

        champion = self.selector.select_deployable_champion(
            candidates, incumbent=self._current_incumbent("cycle"),
            cohort=self.cohort, crossing_id=self.crossing_id,
        )
        # 只返回 deployable champion；没有通过时返回 None（保留 incumbent），
        # 绝不 fallback 到 archive_best。
        if champion is not None:
            return champion

        logger.info(
            "Cycle 进化: 无候选通过 deployable champion 门槛，本轮保持 incumbent"
        )
        return None

    def _evolve_phase(
        self,
        current_code: str,
        paired_cycle_code: str,
        n_candidates: int,
        round_num: int,
    ) -> Optional[ArchiveEntry]:
        """一轮 PhaseSkill 进化。"""
        candidates: List[ArchiveEntry] = []

        archive_summary = self.archive.get_summary(
            self.crossing_id, "phase"
        )
        failure_cases = self._collect_failure_cases("phase")
        constraints_str = self.prompt_builder._format_constraints(self.constraints)

        # 需要一个 CyclePlan 用于 phase skill 评估
        # 先从最佳 cycle skill 获取一个 plan
        paired_plan = self._get_paired_cycle_plan(paired_cycle_code)

        for i in range(n_candidates):
            candidate_id = f"pha_{self.crossing_id}_r{round_num}_c{i}_{uuid.uuid4().hex[:6]}"

            try:
                candidate = self.glm_mutator.mutate_phase_skill(
                    crossing_profile=self.crossing_profile,
                    parent_skill_code=current_code,
                    paired_cycle_skill_code=paired_cycle_code,
                    failure_cases=failure_cases,
                    constraints=constraints_str,
                    archive_summary=archive_summary,
                )
            except Exception as e:
                entry = ArchiveEntry(
                    candidate_id=candidate_id,
                    crossing_id=self.crossing_id,
                    skill_type="phase",
                    code="",
                    rejection_reason=f"GLM 调用失败: {e}",
                    generation=round_num,
                )
                self.archive.add(entry)
                continue

            _, user_prompt = self.prompt_builder.build_phase_prompt(
                crossing_profile=self.crossing_profile,
                parent_code=current_code,
                paired_cycle_code=paired_cycle_code,
                failure_cases=failure_cases,
                constraints=constraints_str,
                archive_summary=archive_summary,
            )

            entry = ArchiveEntry(
                candidate_id=candidate_id,
                crossing_id=self.crossing_id,
                skill_type="phase",
                code=candidate.code,
                prompt=user_prompt[:2000],
                glm_model=getattr(self.glm_mutator.client, "model", "glm"),
                generation=round_num,
            )

            # AST 检查
            ast_result = self.ast_sandbox.check(candidate.code, "phase")
            entry.set_static_check(ast_result)
            if not ast_result.passed:
                entry.rejection_reason = (
                    f"AST 检查失败: {'; '.join(ast_result.violations[:3])}"
                )
                self.archive.add(entry)
                continue

            # Feature Mask 检查（AST 通过后、Replay 评估之前）
            mask_result = self.feature_mask.check_ast_code(candidate.code)
            if not mask_result.passed:
                violation_msgs = "; ".join(v.message for v in mask_result.violations[:3])
                entry.rejection_reason = f"Feature Mask 违规: {violation_msgs}"
                self.archive.add(entry)
                continue

            # Prior Consistency Check（AST 通过后、Replay 评估之前）
            if self.prior_checker is not None:
                prior_result = self.prior_checker.check(candidate.code, "phase")
                entry.set_prior_check(prior_result)
                if not prior_result.passed:
                    entry.rejection_reason = (
                        f"先验一致性检查失败: {'; '.join(prior_result.violations[:3])}"
                    )
                    self.archive.add(entry)
                    continue

            # Replay 评估（需要配对的 CyclePlan）
            replay_report = self.replay_evaluator.evaluate(
                skill_code=candidate.code,
                skill_type="phase",
                crossing_id=self.crossing_id,
                candidate_id=candidate_id,
                paired_plan=paired_plan,
            )
            entry.set_replay_report(replay_report)
            if not replay_report.passed:
                entry.rejection_reason = (
                    f"Replay 评估失败: {'; '.join(replay_report.violations[:3])}"
                )
                self.archive.add(entry)
                continue

            # SUMO 离线评估（仅在 AST + Replay 都通过时执行）
            if self.sumo_evaluator is not None and self.cohort is not None:
                sumo_report = self.sumo_evaluator.evaluate_multi_seed(
                    candidate_code=candidate.code,
                    skill_type="phase",
                    crossing_id=self.crossing_id,
                    cohort=self.cohort,
                )
                entry.set_sumo_report(sumo_report)
                if not sumo_report.passed:
                    entry.rejection_reason = (
                        f"SUMO 评估失败: {'; '.join(sumo_report.violations[:3])}"
                    )
                    self.archive.add(entry)
                    continue

            candidates.append(entry)
            self.archive.add(entry)

        if not candidates:
            return None

        # archive_best 仅用于日志/分析，不能作为部署候选。
        # 理由同 _evolve_cycle：archive_best 未经过 SUMO sealed eval 和
        # non-degradation gate，回退到它会破坏单调进化保证。
        archive_best = self.selector.select_archive_best(
            candidates, seed_entry=None,
        )
        if archive_best is not None:
            logger.info(
                "Phase 进化 archive_best=%s (仅分析用，不参与部署)",
                archive_best.candidate_id,
            )

        champion = self.selector.select_deployable_champion(
            candidates, incumbent=self._current_incumbent("phase"),
            cohort=self.cohort, crossing_id=self.crossing_id,
        )
        # 只返回 deployable champion；没有通过时返回 None（保留 incumbent），
        # 绝不 fallback 到 archive_best。
        if champion is not None:
            return champion

        logger.info(
            "Phase 进化: 无候选通过 deployable champion 门槛，本轮保持 incumbent"
        )
        return None

    # ======================================================================
    # Internal: Helpers
    # ======================================================================

    def _current_incumbent(self, skill_type: str) -> Optional[ArchiveEntry]:
        """返回指定 skill_type 的当前 incumbent（用于 non-degradation gate）。"""
        return self._incumbents.get(skill_type)

    def _register_seed(
        self, seed_code: str, skill_type: str
    ) -> ArchiveEntry:
        """将 seed 代码注册为 archive 中的 generation=0。"""
        candidate_id = f"seed_{self.crossing_id}_{skill_type}_v0"
        return ArchiveEntry(
            candidate_id=candidate_id,
            crossing_id=self.crossing_id,
            skill_type=skill_type,
            code=seed_code,
            generation=0,
            glm_model="seed",
        )

    def _validate_and_evaluate(
        self, entry: ArchiveEntry, skill_type: str
    ) -> bool:
        """对 seed entry 执行 AST 检查和 Replay 评估。"""
        # AST 检查
        ast_result = self.ast_sandbox.check(entry.code, skill_type)
        entry.set_static_check(ast_result)
        if not ast_result.passed:
            entry.rejection_reason = f"Seed AST 检查失败: {'; '.join(ast_result.violations[:3])}"
            return False

        # Replay 评估
        replay_report = self.replay_evaluator.evaluate(
            skill_code=entry.code,
            skill_type=skill_type,
            crossing_id=self.crossing_id,
            candidate_id=entry.candidate_id,
        )
        entry.set_replay_report(replay_report)
        if not replay_report.passed:
            entry.rejection_reason = f"Seed Replay 评估失败: {'; '.join(replay_report.violations[:3])}"
            return False

        return True

    def _collect_failure_cases(self, skill_type: str) -> List[dict]:
        """从历史进化中收集失败案例。"""
        history = self.archive.get_history(self.crossing_id, skill_type)
        failures = []
        for entry in history:
            if entry.replay_report and not entry.replay_report.get("passed", True):
                for fc in entry.replay_report.get("failure_cases", []):
                    failures.append(fc)
        return failures[:20]  # 最多返回 20 个

    def _get_paired_cycle_plan(
        self, cycle_code: str
    ) -> Optional["CyclePlan"]:
        """运行 cycle skill 获取一个 CyclePlan（用于 phase 评估）。"""
        from signalclaw.core.state import (
            CyclePlan,
            NetworkObservation,
            IntersectionObservation,
            PhaseObservation,
            PhaseCommand,
        )

        # 构造一个标准测试场景
        phases = {
            i: PhaseObservation(
                phase_id=i,
                queue=5.0 + i * 2,
                waiting_time=20.0 + i * 5,
                predicted_arrival=3.0,
                elapsed_green=10.0,
                min_green=self.constraints.min_green,
                max_green=self.constraints.max_green,
            )
            for i in range(self.phase_count)
        }
        ego = IntersectionObservation(
            crossing_id=self.crossing_id,
            current_phase_id=0,
            current_phase_elapsed=20.0,
            cycle_second=80.0,
            phases=phases,
            downstream_queue={"e1": 3.0, "e2": 2.0},
            upstream_queue={"e3": 5.0, "e4": 4.0},
        )
        obs = NetworkObservation(ego=ego, neighbors={}, timestamp=100.0)

        try:
            import math
            from collections import deque

            safe_ns = {
                "__builtins__": __builtins__,
                "math": math,
                "deque": deque,
                "dict": dict, "list": list, "tuple": tuple, "set": set,
                "float": float, "int": int, "str": str, "bool": bool,
                "len": len, "min": min, "max": max, "sum": sum, "abs": abs,
                "sorted": sorted, "range": range, "enumerate": enumerate,
                "zip": zip, "round": round, "isinstance": isinstance,
                "any": any, "all": all, "map": map, "filter": filter,
                "reversed": reversed, "print": print,
                "NetworkObservation": NetworkObservation,
                "IntersectionObservation": IntersectionObservation,
                "PhaseObservation": PhaseObservation,
                "CyclePlan": CyclePlan,
                "PhaseCommand": PhaseCommand,
            }
            exec(cycle_code, safe_ns)  # noqa: S102
            if "plan" in safe_ns and callable(safe_ns["plan"]):
                result = safe_ns["plan"](obs)
                if isinstance(result, CyclePlan):
                    return result
        except Exception:
            pass

        # 降级：返回一个默认 plan
        green_times = {
            i: self.constraints.min_green for i in range(self.phase_count)
        }
        return CyclePlan(
            cycle_length=self.constraints.min_green * self.phase_count,
            green_times=green_times,
            phase_order=list(range(self.phase_count)),
        )
