"""SealedTournament - 候选池 + 锦标赛进化系统。

将进化系统从"GLM 生成 1~3 个 candidate -> AST -> replay -> 完成"
改为完整的候选池 + 锦标赛模式：

  每个路口、每种 skill：
    GLM 生成 N 个 DSL/patch candidate
    -> AST/schema/feature mask 筛选
    -> behavior contract 筛选
    -> replay safety 筛选
    -> micro-SUMO 600s 快筛
    -> full-SUMO 3600s paired tournament
    -> 只有通过 non-degradation gate 才能成为 champion

使用方式::

    tournament = SealedTournament(
        crossing_id="123",
        skill_type="cycle",
        config=TournamentConfig(candidates_per_round=30),
        glm_mutator=...,
        dsl_compiler=...,
        ast_sandbox=...,
        replay_evaluator=...,
        sumo_evaluator=...,
        archive=...,
        selector=...,
        constraints=...,
        ...
    )
    result = tournament.run(seed_code=seed_code, incumbent_code=None)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from signalclaw.core.constraints import IntersectionConstraints
from signalclaw.evolution.archive import ArchiveEntry, SkillArchive
from signalclaw.evolution.ast_sandbox import ASTSandbox
from signalclaw.evolution.dsl_compiler import DslCompiler
from signalclaw.evolution.evaluator_replay import ReplayEvaluator
from signalclaw.evolution.evaluator_sumo import SealedSUMOEvaluator
from signalclaw.evolution.feature_mask import FeatureMask
from signalclaw.evolution.glm_mutator import CandidateSkill, GLMSkillMutator
from signalclaw.evolution.prompt_builder import PromptBuilder
from signalclaw.evolution.selector import SkillSelector
from signalclaw.reference.profile_schema import SQLReferenceProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config & Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TournamentConfig:
    """锦标赛配置。"""

    candidates_per_round: int = 30       # GLM 每轮生成多少候选
    micro_sim_duration: float = 600.0    # micro-SUMO 快筛时长（秒）
    full_sim_duration: float = 3600.0    # full-SUMO 完整评估时长（秒）
    non_degradation_threshold: float = 0.02  # 允许退化的比例（2%）
    max_rounds: int = 5                  # 最大进化轮数
    top_k_micro: int = 10                # micro 筛选后保留 top-k
    top_k_paired: int = 3                # paired 筛选后保留 top-k
    use_dsl: bool = True                 # 是否使用 DSL 编译器


@dataclass
class TournamentResult:
    """锦标赛结果（可序列化到 JSON）。"""

    crossing_id: str
    skill_type: str                      # "cycle" or "phase"
    candidate_count: int = 0             # 总候选数
    archive_pass_count: int = 0          # 通过静态+replay筛选的候选数
    sumo_eval_count: int = 0             # 进入 SUMO 评估的候选数
    paired_eval_count: int = 0           # 进入 paired tournament 的候选数
    accepted_champion_count: int = 0     # 通过 non-degradation gate 的候选数
    seed_fallback_count: int = 0         # 回退到 seed 的次数
    champion_score: Optional[float] = None
    champion_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        import json
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


# ---------------------------------------------------------------------------
# SealedTournament
# ---------------------------------------------------------------------------

class SealedTournament:
    """候选池 + 锦标赛进化器。

    每个路口的每种 skill 类型独立运行一次 tournament。
    """

    def __init__(
        self,
        crossing_id: str,
        skill_type: str,
        config: TournamentConfig,
        glm_mutator: GLMSkillMutator,
        prompt_builder: PromptBuilder,
        ast_sandbox: ASTSandbox,
        replay_evaluator: ReplayEvaluator,
        archive: SkillArchive,
        selector: SkillSelector,
        constraints: IntersectionConstraints,
        phase_count: int = 4,
        dsl_compiler: Optional[DslCompiler] = None,
        sumo_evaluator: Optional[SealedSUMOEvaluator] = None,
        cohort: Optional["SkillCohort"] = None,
        sql_profile: Optional[SQLReferenceProfile] = None,
        feature_mask: Optional[FeatureMask] = None,
        paired_skill_code: Optional[str] = None,
    ):
        self.crossing_id = crossing_id
        self.skill_type = skill_type
        self.config = config
        self.glm_mutator = glm_mutator
        self.prompt_builder = prompt_builder
        self.ast_sandbox = ast_sandbox
        self.replay_evaluator = replay_evaluator
        self.archive = archive
        self.selector = selector
        self.constraints = constraints
        self.phase_count = phase_count
        self.dsl_compiler = dsl_compiler
        self.sumo_evaluator = sumo_evaluator
        self.cohort = cohort
        self.sql_profile = sql_profile
        self.feature_mask = feature_mask or FeatureMask()
        self.paired_skill_code = paired_skill_code

        # incumbent 跟踪
        self._incumbent: Optional[ArchiveEntry] = None

    # ==================================================================
    # Public API
    # ==================================================================

    def run(
        self,
        seed_code: str,
        incumbent_code: Optional[str] = None,
        round_num: int = 1,
    ) -> TournamentResult:
        """执行完整 tournament 流程。

        Parameters
        ----------
        seed_code : str
            seed skill 代码
        incumbent_code : str, optional
            当前 champion 代码。如果为 None，使用 seed_code 作为 incumbent。
        round_num : int
            当前进化轮数（用于 generation 标记）

        Returns
        -------
        TournamentResult
        """
        result = TournamentResult(
            crossing_id=self.crossing_id,
            skill_type=self.skill_type,
        )

        # 确定 incumbent 代码
        inc_code = incumbent_code or seed_code

        # 注册 seed 到 archive
        seed_entry = self._register_seed(seed_code)
        seed_valid = self._validate_seed(seed_entry)
        if seed_valid:
            self._incumbent = seed_entry
        else:
            logger.warning(
                "Seed skill 未通过验证，tournament 继续但 incumbent 为空"
            )

        # ---- Phase 1: 生成候选 ----
        logger.info(
            "[tournament] %s/%s round=%d: 开始生成 %d 个候选",
            self.crossing_id, self.skill_type, round_num,
            self.config.candidates_per_round,
        )
        candidates = self._generate_candidates(
            seed_code, inc_code, round_num,
        )
        result.candidate_count = len(candidates)
        logger.info(
            "[tournament] %s/%s: 生成了 %d 个候选",
            self.crossing_id, self.skill_type, len(candidates),
        )

        if not candidates:
            result.seed_fallback_count = 1
            return result

        # ---- Phase 2: 静态筛选 (AST + schema + feature mask) ----
        passed_static = self._filter_static(candidates)
        logger.info(
            "[tournament] %s/%s: 静态筛选 %d -> %d",
            self.crossing_id, self.skill_type,
            len(candidates), len(passed_static),
        )

        # ---- Phase 3: behavior contract 筛选 ----
        passed_behavior = self._filter_behavior(passed_static)
        logger.info(
            "[tournament] %s/%s: Behavior contract %d -> %d",
            self.crossing_id, self.skill_type,
            len(passed_static), len(passed_behavior),
        )

        # ---- Phase 4: replay safety 筛选 ----
        passed_replay = self._filter_replay(passed_behavior)
        result.archive_pass_count = len(passed_replay)
        logger.info(
            "[tournament] %s/%s: Replay 筛选 %d -> %d",
            self.crossing_id, self.skill_type,
            len(passed_behavior), len(passed_replay),
        )

        if not passed_replay:
            result.seed_fallback_count = 1
            return result

        # ---- Phase 5: micro-SUMO 快筛 ----
        passed_micro = self._filter_micro_sumo(passed_replay)
        result.sumo_eval_count = len(passed_micro)
        logger.info(
            "[tournament] %s/%s: Micro-SUMO %d -> %d",
            self.crossing_id, self.skill_type,
            len(passed_replay), len(passed_micro),
        )

        # ---- Phase 6: full-SUMO paired tournament ----
        passed_paired = self._filter_paired_sumo(passed_micro, inc_code)
        result.paired_eval_count = len(passed_paired)
        logger.info(
            "[tournament] %s/%s: Paired-SUMO %d -> %d",
            self.crossing_id, self.skill_type,
            len(passed_micro), len(passed_paired),
        )

        # ---- Phase 7: 选择 champion ----
        champion = self._select_champion(passed_paired, inc_code)
        if champion is not None:
            result.accepted_champion_count = 1
            result.champion_id = champion.candidate_id
            result.champion_score = (
                champion.sumo_report.get("score")
                if champion.sumo_report else None
            )
            self._incumbent = champion
            logger.info(
                "[tournament] %s/%s: Champion = %s (score=%s)",
                self.crossing_id, self.skill_type,
                champion.candidate_id, result.champion_score,
            )
        else:
            result.seed_fallback_count = 1
            logger.info(
                "[tournament] %s/%s: 无候选通过 non-degradation gate",
                self.crossing_id, self.skill_type,
            )

        return result

    @property
    def incumbent(self) -> Optional[ArchiveEntry]:
        """当前 champion（seed 或上一代 champion）。"""
        return self._incumbent

    # ==================================================================
    # Phase 1: 候选生成
    # ==================================================================

    def _generate_candidates(
        self,
        seed_code: str,
        incumbent_code: str,
        round_num: int,
    ) -> List[ArchiveEntry]:
        """使用 GLM 生成 N 个 DSL/patch 候选。"""
        candidates: List[ArchiveEntry] = []
        n = self.config.candidates_per_round

        # 获取历史信息用于 prompt
        archive_summary = self.archive.get_summary(
            self.crossing_id, self.skill_type,
        )
        failure_cases = self._collect_failure_cases()
        constraints_str = self.prompt_builder._format_constraints(self.constraints)

        for i in range(n):
            candidate_id = (
                f"t_{self.skill_type[:3]}_{self.crossing_id}"
                f"_r{round_num}_c{i}_{uuid.uuid4().hex[:6]}"
            )

            try:
                if self.skill_type == "cycle":
                    raw_candidate: CandidateSkill = self.glm_mutator.mutate_cycle_skill(
                        crossing_profile=self._build_crossing_profile(),
                        parent_skill_code=incumbent_code,
                        failure_cases=failure_cases,
                        constraints=constraints_str,
                        archive_summary=archive_summary,
                    )
                else:
                    paired_cycle_code = self.paired_skill_code or seed_code
                    raw_candidate = self.glm_mutator.mutate_phase_skill(
                        crossing_profile=self._build_crossing_profile(),
                        parent_skill_code=incumbent_code,
                        paired_cycle_skill_code=paired_cycle_code,
                        failure_cases=failure_cases,
                        constraints=constraints_str,
                        archive_summary=archive_summary,
                    )
            except Exception as e:
                logger.warning(
                    "候选 %d/%d GLM 调用失败: %s", i + 1, n, e,
                )
                entry = ArchiveEntry(
                    candidate_id=candidate_id,
                    crossing_id=self.crossing_id,
                    skill_type=self.skill_type,
                    code="",
                    rejection_reason=f"GLM 调用失败: {e}",
                    generation=round_num,
                )
                self.archive.add(entry)
                continue

            # 尝试 DSL 编译（如果启用了 DSL 模式）
            code = raw_candidate.code
            if self.config.use_dsl and self.dsl_compiler is not None:
                code = self._try_dsl_compile(raw_candidate.code)

            if not code:
                logger.debug("候选 %d: 代码为空，跳过", i)
                entry = ArchiveEntry(
                    candidate_id=candidate_id,
                    crossing_id=self.crossing_id,
                    skill_type=self.skill_type,
                    code="",
                    rejection_reason="GLM 返回空代码",
                    generation=round_num,
                )
                self.archive.add(entry)
                continue

            entry = ArchiveEntry(
                candidate_id=candidate_id,
                crossing_id=self.crossing_id,
                skill_type=self.skill_type,
                code=code,
                generation=round_num,
                glm_model=getattr(self.glm_mutator.client, "model", "glm"),
            )
            candidates.append(entry)

        return candidates

    def _try_dsl_compile(self, raw_code: str) -> str:
        """尝试将 GLM 输出作为 DSL 编译；失败时返回原始代码作为 fallback。"""
        try:
            result = self.dsl_compiler.compile(raw_code)
            if result.success and result.python_code:
                return result.python_code
        except Exception:
            pass
        # DSL 编译失败，返回原始代码（可能是直接 Python）
        return raw_code

    # ==================================================================
    # Phase 2: 静态筛选 (AST + schema + feature mask)
    # ==================================================================

    def _filter_static(
        self, candidates: List[ArchiveEntry],
    ) -> List[ArchiveEntry]:
        """AST + schema + feature mask 筛选。"""
        passed: List[ArchiveEntry] = []

        for entry in candidates:
            if not entry.code:
                continue

            # AST 检查
            ast_result = self.ast_sandbox.check(entry.code, self.skill_type)
            entry.set_static_check(ast_result)
            if not ast_result.passed:
                entry.rejection_reason = (
                    f"AST 检查失败: {'; '.join(ast_result.violations[:3])}"
                )
                self.archive.add(entry)
                continue

            # Feature Mask 检查（AST 通过后）
            mask_result = self.feature_mask.check_ast_code(entry.code)
            if not mask_result.passed:
                violation_msgs = "; ".join(
                    v.message for v in mask_result.violations[:3]
                )
                entry.rejection_reason = f"Feature Mask 违规: {violation_msgs}"
                self.archive.add(entry)
                continue

            passed.append(entry)

        return passed

    # ==================================================================
    # Phase 3: behavior contract 筛选
    # ==================================================================

    def _filter_behavior(
        self, candidates: List[ArchiveEntry],
    ) -> List[ArchiveEntry]:
        """behavior contract 筛选。

        需要 seed skill 作为参考。如果没有 seed skill 或无法创建 checker，
        则跳过此阶段（所有候选通过）。
        """
        if self._incumbent is None or not self._incumbent.code:
            # 无 seed，跳过 behavior contract 筛选
            return candidates

        try:
            from signalclaw.evolution.behavior_contracts import (
                BehaviorContractChecker,
                GoldenObservationSet,
            )
        except ImportError:
            logger.warning("无法导入 behavior_contracts，跳过 behavior 筛选")
            return candidates

        # 加载 seed 和 candidate 的命名空间
        seed_ns = self._load_skill_ns(self._incumbent.code)
        if seed_ns is None:
            return candidates

        checker = BehaviorContractChecker()
        passed: List[ArchiveEntry] = []

        # 创建 golden observation set
        golden_obs = self._build_golden_observations()
        if golden_obs is None:
            return candidates

        paired_plan = self._get_paired_plan()

        for entry in candidates:
            cand_ns = self._load_skill_ns(entry.code)
            if cand_ns is None:
                entry.rejection_reason = "代码加载失败（behavior contract）"
                self.archive.add(entry)
                continue

            contract_result = checker.check_contracts(
                seed_skill=seed_ns,
                candidate_skill=cand_ns,
                golden_obs_set=golden_obs,
                skill_type=self.skill_type,
                paired_plan=paired_plan,
            )

            if not contract_result.passed:
                violations_str = "; ".join(
                    str(v) for v in contract_result.violations[:3]
                )
                entry.rejection_reason = (
                    f"Behavior contract 失败: {violations_str}"
                )
                self.archive.add(entry)
                continue

            passed.append(entry)

        return passed

    # ==================================================================
    # Phase 4: replay safety 筛选
    # ==================================================================

    def _filter_replay(
        self, candidates: List[ArchiveEntry],
    ) -> List[ArchiveEntry]:
        """replay safety 筛选。"""
        passed: List[ArchiveEntry] = []
        paired_plan = self._get_paired_plan()

        for entry in candidates:
            replay_report = self.replay_evaluator.evaluate(
                skill_code=entry.code,
                skill_type=self.skill_type,
                crossing_id=self.crossing_id,
                candidate_id=entry.candidate_id,
                paired_plan=paired_plan,
            )
            entry.set_replay_report(replay_report)

            if not replay_report.passed:
                entry.rejection_reason = (
                    f"Replay 评估失败: {'; '.join(replay_report.violations[:3])}"
                )
                self.archive.add(entry)
                continue

            passed.append(entry)
            self.archive.add(entry)

        return passed

    # ==================================================================
    # Phase 5: micro-SUMO 快筛
    # ==================================================================

    def _filter_micro_sumo(
        self, candidates: List[ArchiveEntry],
    ) -> List[ArchiveEntry]:
        """600s micro-SUMO 快筛，保留 top-k。

        如果没有 SUMO evaluator 或 cohort，直接返回所有通过 replay 的候选。
        """
        if self.sumo_evaluator is None or self.cohort is None:
            logger.info(
                "[tournament] 无 SUMO evaluator，跳过 micro-SUMO 筛选"
            )
            return candidates[: self.config.top_k_micro]

        # 临时修改评估时长为 micro duration
        original_duration = self.sumo_evaluator._inner.eval_duration
        self.sumo_evaluator._inner.eval_duration = self.config.micro_sim_duration

        try:
            scored: List[tuple] = []

            for entry in candidates:
                try:
                    report = self.sumo_evaluator.evaluate_multi_seed(
                        candidate_code=entry.code,
                        skill_type=self.skill_type,
                        crossing_id=self.crossing_id,
                        cohort=self.cohort,
                    )
                    entry.set_sumo_report(report)

                    if report.score == float("inf"):
                        entry.rejection_reason = "Micro-SUMO 评估失败"
                        self.archive.add(entry)
                        continue

                    scored.append((entry, report.score))

                except Exception as e:
                    logger.warning(
                        "候选 %s micro-SUMO 异常: %s",
                        entry.candidate_id, e,
                    )
                    entry.rejection_reason = f"Micro-SUMO 异常: {e}"
                    self.archive.add(entry)

        finally:
            # 恢复原始评估时长
            self.sumo_evaluator._inner.eval_duration = original_duration

        if not scored:
            return []

        # 按 score 排序（越低越好），取 top-k
        scored.sort(key=lambda x: x[1])
        top_k = scored[: self.config.top_k_micro]

        logger.info(
            "[tournament] micro-SUMO top-%d: scores=%s",
            len(top_k),
            [f"{s:.4f}" for _, s in top_k],
        )

        return [entry for entry, _ in top_k]

    # ==================================================================
    # Phase 6: full-SUMO paired tournament
    # ==================================================================

    def _filter_paired_sumo(
        self,
        candidates: List[ArchiveEntry],
        incumbent_code: str,
    ) -> List[ArchiveEntry]:
        """3600s full-SUMO paired tournament，与 incumbent 对比。

        如果没有 SUMO evaluator 或 cohort，直接返回所有候选（降级模式）。
        """
        if self.sumo_evaluator is None or self.cohort is None:
            logger.info(
                "[tournament] 无 SUMO evaluator，跳过 paired-SUMO 筛选"
            )
            return candidates[: self.config.top_k_paired]

        # 临时修改评估时长为 full duration
        original_duration = self.sumo_evaluator._inner.eval_duration
        self.sumo_evaluator._inner.eval_duration = self.config.full_sim_duration

        passed: List[ArchiveEntry] = []

        try:
            for entry in candidates:
                try:
                    paired_report = self.sumo_evaluator.paired_evaluate(
                        incumbent_code=incumbent_code,
                        candidate_code=entry.code,
                        skill_type=self.skill_type,
                        crossing_id=self.crossing_id,
                        cohort=self.cohort,
                    )

                    # 将 paired report 的 candidate 部分写入 entry
                    entry.set_sumo_report_from_paired(paired_report)

                    if paired_report.passed:
                        entry.paired_eval_passed = True
                        passed.append(entry)
                    else:
                        entry.paired_eval_passed = False
                        entry.deployment_rejection_reason = (
                            paired_report.rejection_reason
                        )

                    self.archive.add(entry)

                except Exception as e:
                    logger.warning(
                        "候选 %s paired-SUMO 异常: %s",
                        entry.candidate_id, e,
                    )
                    entry.rejection_reason = f"Paired-SUMO 异常: {e}"
                    self.archive.add(entry)

        finally:
            # 恢复原始评估时长
            self.sumo_evaluator._inner.eval_duration = original_duration

        # 按 champion score 排序，取 top-k
        if len(passed) > self.config.top_k_paired:
            scored = []
            for entry in passed:
                score = self._compute_entry_score(entry)
                scored.append((entry, score))
            scored.sort(key=lambda x: x[1])
            passed = [e for e, _ in scored[: self.config.top_k_paired]]

        logger.info(
            "[tournament] paired-SUMO: %d/%d 通过 non-degradation gate",
            len(passed), len(candidates),
        )

        return passed

    # ==================================================================
    # Phase 7: 选择 champion
    # ==================================================================

    def _select_champion(
        self,
        candidates: List[ArchiveEntry],
        incumbent_code: str,
    ) -> Optional[ArchiveEntry]:
        """选择最佳 champion。只有通过 non-degradation gate 的才能入选。

        如果没有候选，返回 None（表示回退到 seed/incumbent）。
        """
        if not candidates:
            return None

        # 如果只有一个候选，直接返回
        if len(candidates) == 1:
            c = candidates[0]
            incumbent_id = (
                self._incumbent.candidate_id
                if self._incumbent else None
            )
            c.mark_deployable_champion(incumbent_skill_id=incumbent_id)
            return c

        # 多个候选：按 score 排序选最优
        scored = []
        for entry in candidates:
            score = self._compute_entry_score(entry)
            scored.append((entry, score))
        scored.sort(key=lambda x: x[1])

        champion = scored[0][0]
        incumbent_id = (
            self._incumbent.candidate_id
            if self._incumbent else None
        )
        champion.mark_deployable_champion(incumbent_skill_id=incumbent_id)

        # 标记其他候选为被拒绝
        for entry, score in scored[1:]:
            entry.mark_rejected(
                reason=f"软排序劣于 champion {champion.candidate_id}",
                incumbent_skill_id=incumbent_id,
            )

        return champion

    # ==================================================================
    # Helpers
    # ==================================================================

    def _build_crossing_profile(self) -> str:
        """构造路口拓扑描述。"""
        c = self.constraints
        return (
            f"路口 ID: {self.crossing_id}\n"
            f"相位数量: {self.phase_count}\n"
            f"最小绿灯: {c.min_green}s\n"
            f"最大绿灯: {c.max_green}s\n"
            f"最小周期: {c.min_cycle}s\n"
            f"最大周期: {c.max_cycle}s\n"
            f"黄灯时间: {c.yellow_time}s\n"
            f"全红时间: {c.all_red_time}s\n"
            f"最大延长: {c.max_extend}s\n"
            f"最大缩短: {c.max_shorten}s\n"
            f"强制相位: {c.force_phase_ids or '无'}\n"
        )

    def _register_seed(self, seed_code: str) -> ArchiveEntry:
        """将 seed 注册到 archive。"""
        candidate_id = f"seed_{self.crossing_id}_{self.skill_type}_v0"
        entry = ArchiveEntry(
            candidate_id=candidate_id,
            crossing_id=self.crossing_id,
            skill_type=self.skill_type,
            code=seed_code,
            generation=0,
            glm_model="seed",
        )
        return entry

    def _validate_seed(self, entry: ArchiveEntry) -> bool:
        """验证 seed 是否通过基本检查。"""
        # AST
        ast_result = self.ast_sandbox.check(entry.code, self.skill_type)
        entry.set_static_check(ast_result)
        if not ast_result.passed:
            entry.rejection_reason = (
                f"Seed AST 检查失败: {'; '.join(ast_result.violations[:3])}"
            )
            return False

        # Replay
        replay_report = self.replay_evaluator.evaluate(
            skill_code=entry.code,
            skill_type=self.skill_type,
            crossing_id=self.crossing_id,
            candidate_id=entry.candidate_id,
        )
        entry.set_replay_report(replay_report)
        if not replay_report.passed:
            entry.rejection_reason = (
                f"Seed Replay 失败: {'; '.join(replay_report.violations[:3])}"
            )
            return False

        entry.selected = True
        self.archive.add(entry)
        return True

    def _collect_failure_cases(self) -> List[dict]:
        """从历史进化中收集失败案例。"""
        history = self.archive.get_history(
            self.crossing_id, self.skill_type,
        )
        failures = []
        for entry in history:
            if entry.replay_report and not entry.replay_report.get(
                "passed", True,
            ):
                for fc in entry.replay_report.get("failure_cases", []):
                    failures.append(fc)
        return failures[:20]

    def _get_paired_plan(self):
        """获取配对的 CyclePlan（用于 phase skill 评估）。"""
        if self.skill_type != "phase":
            return None

        paired_code = self.paired_skill_code
        if not paired_code:
            return None

        from signalclaw.core.state import (
            CyclePlan,
            NetworkObservation,
            IntersectionObservation,
            PhaseObservation,
            PhaseCommand,
        )

        # 构造标准测试场景
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
            exec(paired_code, safe_ns)
            if "plan" in safe_ns and callable(safe_ns["plan"]):
                result = safe_ns["plan"](obs)
                if isinstance(result, CyclePlan):
                    return result
        except Exception:
            pass

        # 降级：返回默认 plan
        return CyclePlan(
            cycle_length=self.constraints.min_green * self.phase_count,
            green_times={
                i: self.constraints.min_green
                for i in range(self.phase_count)
            },
            phase_order=list(range(self.phase_count)),
        )

    def _load_skill_ns(self, code: str):
        """加载 skill 代码到命名空间。"""
        from signalclaw.core.state import (
            CyclePlan,
            NetworkObservation,
            IntersectionObservation,
            PhaseObservation,
            PhaseCommand,
        )
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

        try:
            exec(code, safe_ns)
        except Exception:
            return None

        # 验证函数存在
        if self.skill_type == "cycle":
            if "plan" not in safe_ns or not callable(safe_ns["plan"]):
                return None
        elif self.skill_type == "phase":
            if "decide" not in safe_ns or not callable(safe_ns["decide"]):
                return None

        return safe_ns

    def _build_golden_observations(self):
        """构建 golden observation set（用于 behavior contract 检查）。"""
        try:
            from signalclaw.evolution.behavior_contracts import (
                GoldenObservationSet,
            )
            from signalclaw.core.state import (
                NetworkObservation,
                IntersectionObservation,
                PhaseObservation,
            )

            obs_list = []
            # 正常场景
            for current_phase in range(self.phase_count):
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
                    current_phase_id=current_phase,
                    current_phase_elapsed=15.0,
                    cycle_second=80.0,
                    phases=phases,
                    downstream_queue={"e1": 3.0, "e2": 2.0},
                    upstream_queue={"e3": 5.0, "e4": 4.0},
                )
                obs = NetworkObservation(
                    ego=ego, neighbors={}, timestamp=100.0,
                )
                obs_list.append(obs)

            # 高峰场景
            phases_peak = {
                i: PhaseObservation(
                    phase_id=i,
                    queue=30.0 + i * 5,
                    waiting_time=60.0 + i * 10,
                    predicted_arrival=3.0,
                    elapsed_green=25.0,
                    min_green=self.constraints.min_green,
                    max_green=self.constraints.max_green,
                )
                for i in range(self.phase_count)
            }
            ego_peak = IntersectionObservation(
                crossing_id=self.crossing_id,
                current_phase_id=0,
                current_phase_elapsed=25.0,
                cycle_second=120.0,
                phases=phases_peak,
                downstream_queue={"e1": 20.0, "e2": 15.0},
                upstream_queue={"e3": 25.0, "e4": 20.0},
            )
            obs_list.append(
                NetworkObservation(
                    ego=ego_peak, neighbors={}, timestamp=200.0,
                )
            )

            return GoldenObservationSet(observations=obs_list)

        except Exception as e:
            logger.warning("构建 GoldenObservationSet 失败: %s", e)
            return None

    def _compute_entry_score(self, entry: ArchiveEntry) -> float:
        """计算 entry 的综合分数（越低越好）。"""
        score = 0.0

        # replay score 贡献
        if entry.replay_report:
            replay_score = entry.replay_report.get("score", 0.0)
            score -= replay_score * 10.0

        # SUMO metrics 贡献
        if entry.sumo_report and entry.sumo_report.get("metrics"):
            metrics = entry.sumo_report["metrics"]
            score += metrics.get("mean_waiting", 0.0) * 1.0
            score += metrics.get("mean_queue", 0.0) * 1.0
            score -= metrics.get("throughput", 0.0) * 0.6

        return score
