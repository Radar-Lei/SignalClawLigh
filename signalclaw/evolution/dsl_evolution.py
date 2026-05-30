"""DSLEvolutionPipeline - 完整的 DSL 进化流程。

整合 GLM DSL 结构生成 + 参数优化 + Sealed Evaluation 的三阶段流水线：
  Stage 1: GLM 提出 DSL 结构模板（feature 组合、公式结构、保护规则）
  Stage 2: 参数优化器搜索最优参数组合（权重、阈值等连续参数）
  Stage 3: Sealed SUMO evaluation 验证最终结果（确认比 incumbent 好）

设计理念：
  - GLM 擅长：离散结构决策（选哪些 feature、用什么分配方法、设什么 guard）
  - 参数优化器擅长：连续参数调优（精确调权重、阈值）
  - Sealed Evaluator 负责：真实仿真环境下的最终判定

使用方式::

    pipeline = DSLEvolutionPipeline(
        glm_mutator=glm_mutator,
        param_optimizer_config=OptimizerConfig(method="bayesian", n_trials=50),
        sumo_evaluator=sumo_evaluator,
        cohort=cohort,
    )
    result = pipeline.evolve_skill(
        crossing_id="123",
        skill_type="cycle",
        incumbent_dsl=incumbent_yaml,
        incumbent_score=10.5,
    )
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from signalclaw.evolution.dsl_compiler import (
    CompileResult,
    DslCompiler,
    DslSchema,
    DslValidationResult,
)
from signalclaw.evolution.evaluator_sumo import (
    SealedSUMOEvaluator,
    SUMOEvalReport,
    PairedEvalReport,
)
from signalclaw.evolution.param_optimizer import (
    CYCLE_OPTIMIZABLE_PARAMS,
    DEFAULT_PARAM_RANGES,
    PHASE_OPTIMIZABLE_PARAMS,
    DSLParamOptimizer,
    OptimizerConfig,
    OptimizationResult,
    QuickParamScreener,
)
from signalclaw.skills.cohort import SkillCohort

logger = logging.getLogger(__name__)


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class DSLStructureProposal:
    """GLM 提出的 DSL 结构方案。"""

    proposal_id: str
    dsl_template: dict  # DSL 结构（参数可留默认值）
    rationale: str  # GLM 的改进思路说明
    skill_type: str  # "cycle" | "phase"
    compile_ok: bool = False  # 结构本身能否编译通过
    validation_errors: List[str] = field(default_factory=list)


@dataclass
class EvolutionStageResult:
    """单阶段结果。"""

    stage_name: str
    success: bool
    duration_sec: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillEvolutionResult:
    """完整的 Skill 进化结果（三阶段）。"""

    crossing_id: str
    skill_type: str
    passed: bool  # 是否通过 sealed evaluation
    final_score: float  # 最终分数
    incumbent_score: float
    improvement_pct: float  # 改善百分比（正值表示改善）
    n_proposals_tried: int  # 尝试了多少个 GLM 方案
    best_params: Dict[str, float]  # 最优参数
    best_dsl_yaml: str  # 最优 DSL YAML
    best_python_code: str  # 最优 Python 代码
    stage_results: List[EvolutionStageResult] = field(default_factory=list)
    sealed_report: Optional[Dict[str, Any]] = None  # PairedEvalReport 的字典

    @property
    def improved(self) -> bool:
        """是否比 incumbent 有改善。"""
        return self.final_score < self.incumbent_score


# ============================================================================
# DSL 模板修复器
# ============================================================================

def _ensure_dsl_defaults(dsl: dict, skill_type: str) -> dict:
    """确保 DSL 模板包含必要的默认字段。"""
    dsl = copy.deepcopy(dsl)

    # 确保基本字段
    dsl.setdefault("skill_type", skill_type)
    dsl.setdefault("version_note", "param_optimized")
    dsl.setdefault("parameters", {})

    if skill_type == "cycle":
        dsl.setdefault("features_used", ["queue"])
        dsl.setdefault("cycle", {})
        dsl["cycle"].setdefault("base", 80.0)
        dsl["cycle"].setdefault("queue_gain", 0.5)
        dsl["cycle"].setdefault("min", 40.0)
        dsl["cycle"].setdefault("max", 180.0)
        dsl.setdefault("allocation", {})
        dsl["allocation"].setdefault("method", "softmax")
        dsl["allocation"].setdefault("min_green", 10.0)
        dsl["allocation"].setdefault("max_green", 60.0)
        dsl.setdefault("guards", {})
        dsl["guards"].setdefault("all_phases_served", True)
        dsl["guards"].setdefault("max_cycle_jump", 20)
        dsl["guards"].setdefault("downstream_block_clip", True)

    # 确保 w_queue 存在（必填）
    dsl["parameters"].setdefault("w_queue", 1.0)

    return dsl


# ============================================================================
# DSLEvolutionPipeline
# ============================================================================

class DSLEvolutionPipeline:
    """完整的 DSL 进化流水线：GLM 结构 → 参数优化 → Sealed 评估。

    Parameters
    ----------
    glm_propose_fn : callable, optional
        GLM DSL 结构生成函数。
        签名: (crossing_id: str, skill_type: str, incumbent_dsl: dict,
               n_proposals: int) -> List[DSLStructureProposal]
        如果为 None，则跳过 GLM 阶段，直接对 incumbent DSL 做参数优化。
    param_optimizer_config : OptimizerConfig
        参数优化器配置
    sealed_evaluator : SealedSUMOEvaluator
        Sealed SUMO 评估器
    cohort : SkillCohort
        Skill 集合（用于评估时替换目标路口的 skill）
    compiler : DslCompiler, optional
        DSL 编译器，为 None 时使用默认实例
    n_proposals : int
        每轮进化生成几个 GLM 方案
    max_rounds : int
        最大进化轮数
    """

    def __init__(
        self,
        glm_propose_fn: Optional[Callable] = None,
        param_optimizer_config: Optional[OptimizerConfig] = None,
        sealed_evaluator: Optional[SealedSUMOEvaluator] = None,
        cohort: Optional[SkillCohort] = None,
        compiler: Optional[DslCompiler] = None,
        n_proposals: int = 3,
        max_rounds: int = 2,
    ):
        self.glm_propose_fn = glm_propose_fn
        self.param_optimizer_config = param_optimizer_config or OptimizerConfig()
        self.sealed_evaluator = sealed_evaluator
        self.cohort = cohort
        self.compiler = compiler or DslCompiler()
        self.n_proposals = n_proposals
        self.max_rounds = max_rounds
        self.screener = QuickParamScreener(self.compiler)

    # ==================================================================
    # 主入口
    # ==================================================================

    def evolve_skill(
        self,
        crossing_id: str,
        skill_type: str,
        incumbent_dsl: dict,
        incumbent_score: float,
        incumbent_code: str = "",
    ) -> SkillEvolutionResult:
        """执行完整的 DSL 进化流程。

        Parameters
        ----------
        crossing_id : str
            目标路口 ID
        skill_type : str
            "cycle" 或 "phase"
        incumbent_dsl : dict
            当前 incumbent 的 DSL（YAML 解析后的 dict）
        incumbent_score : float
            当前 incumbent 的分数（越低越好）
        incumbent_code : str
            当前 incumbent 的 Python 代码（用于 sealed evaluation）

        Returns
        -------
        SkillEvolutionResult
        """
        start_time = time.time()
        stage_results: List[EvolutionStageResult] = []

        # 确保 incumbent DSL 格式正确
        incumbent_dsl = _ensure_dsl_defaults(incumbent_dsl, skill_type)

        best_overall_result: Optional[OptimizationResult] = None
        best_overall_code = ""
        best_overall_dsl = incumbent_dsl
        n_proposals_tried = 0

        # ---- 多轮进化 ----
        for round_num in range(1, self.max_rounds + 1):
            logger.info(
                "DSL 进化 Round %d/%d: crossing=%s type=%s",
                round_num, self.max_rounds, crossing_id, skill_type,
            )

            # ---- Stage 1: 获取 DSL 结构方案 ----
            stage1_start = time.time()
            proposals = self._get_proposals(
                crossing_id, skill_type, incumbent_dsl,
            )
            stage1_duration = time.time() - stage1_start
            stage_results.append(EvolutionStageResult(
                stage_name=f"round{round_num}_glm_propose",
                success=len(proposals) > 0,
                duration_sec=stage1_duration,
                details={"n_proposals": len(proposals)},
            ))

            if not proposals:
                logger.warning("Round %d: 无可用 DSL 方案", round_num)
                # 使用 incumbent DSL 继续参数优化
                proposals = [DSLStructureProposal(
                    proposal_id=f"incumbent_fallback_r{round_num}",
                    dsl_template=incumbent_dsl,
                    rationale="incumbent fallback（无 GLM 方案）",
                    skill_type=skill_type,
                    compile_ok=True,
                )]

            # ---- Stage 2: 对每个方案做参数优化 ----
            for proposal in proposals:
                n_proposals_tried += 1

                if not proposal.compile_ok:
                    logger.info(
                        "跳过不可编译的方案: %s (%s)",
                        proposal.proposal_id, proposal.validation_errors,
                    )
                    continue

                stage2_start = time.time()
                opt_result = self._optimize_params(
                    proposal, incumbent_score, skill_type,
                )
                stage2_duration = time.time() - stage2_start
                stage_results.append(EvolutionStageResult(
                    stage_name=f"round{round_num}_param_opt_{proposal.proposal_id}",
                    success=opt_result.improved,
                    duration_sec=stage2_duration,
                    details={
                        "best_score": opt_result.best_score,
                        "n_evaluations": opt_result.n_evaluations,
                        "method": opt_result.optimization_method,
                    },
                ))

                if opt_result.improved:
                    if (best_overall_result is None or
                            opt_result.best_score < best_overall_result.best_score):
                        best_overall_result = opt_result
                        best_overall_code = opt_result.best_python_code
                        best_overall_dsl = proposal.dsl_template
                        logger.info(
                            "Round %d 方案 %s: 新最优 score=%.4f (improvement=%.2f%%)",
                            round_num, proposal.proposal_id,
                            opt_result.best_score, opt_result.improvement_pct,
                        )

        # ---- Stage 3: Sealed Evaluation ----
        final_score = incumbent_score
        sealed_report_dict = None
        passed = False

        if best_overall_result and best_overall_code:
            stage3_start = time.time()
            passed, sealed_report_dict, final_score = self._sealed_evaluate(
                crossing_id, skill_type, incumbent_code, best_overall_code,
            )
            stage3_duration = time.time() - stage3_start
            stage_results.append(EvolutionStageResult(
                stage_name="sealed_evaluation",
                success=passed,
                duration_sec=stage3_duration,
                details={"final_score": final_score},
            ))
        else:
            stage_results.append(EvolutionStageResult(
                stage_name="sealed_evaluation",
                success=False,
                details={"reason": "无改善方案，跳过 sealed evaluation"},
            ))

        total_duration = time.time() - start_time
        logger.info(
            "DSL 进化完成: crossing=%s type=%s passed=%s "
            "score=%.4f->%.4f (%.2f%%) n_proposals=%d duration=%.1fs",
            crossing_id, skill_type, passed,
            incumbent_score, final_score,
            ((incumbent_score - final_score) / max(abs(incumbent_score), 1e-9)) * 100,
            n_proposals_tried, total_duration,
        )

        # 计算改善百分比
        if incumbent_score > 0:
            improvement_pct = (incumbent_score - final_score) / incumbent_score * 100
        else:
            improvement_pct = 0.0

        # 生成最终 DSL YAML
        best_params = best_overall_result.best_params if best_overall_result else {}
        best_dsl_yaml = ""
        best_python_code = ""
        if best_overall_result:
            best_dsl_yaml = best_overall_result.best_dsl_yaml
            best_python_code = best_overall_result.best_python_code

        return SkillEvolutionResult(
            crossing_id=crossing_id,
            skill_type=skill_type,
            passed=passed,
            final_score=final_score,
            incumbent_score=incumbent_score,
            improvement_pct=round(improvement_pct, 2),
            n_proposals_tried=n_proposals_tried,
            best_params=best_params,
            best_dsl_yaml=best_dsl_yaml,
            best_python_code=best_python_code,
            stage_results=stage_results,
            sealed_report=sealed_report_dict,
        )

    # ==================================================================
    # 内部方法
    # ==================================================================

    def _get_proposals(
        self,
        crossing_id: str,
        skill_type: str,
        incumbent_dsl: dict,
    ) -> List[DSLStructureProposal]:
        """获取 GLM 提出的 DSL 结构方案。

        如果没有 GLM 函数，返回一个基于 incumbent 的模板。
        """
        if self.glm_propose_fn is None:
            # 无 GLM，直接用 incumbent 作为模板
            template = copy.deepcopy(incumbent_dsl)
            # 验证模板可以编译
            dsl_yaml = yaml.dump(template, default_flow_style=False, allow_unicode=True)
            vr = self.compiler.validate(dsl_yaml)
            return [DSLStructureProposal(
                proposal_id=f"incumbent_template_{uuid.uuid4().hex[:6]}",
                dsl_template=template,
                rationale="incumbent DSL 模板（无 GLM 生成）",
                skill_type=skill_type,
                compile_ok=vr.valid,
                validation_errors=vr.errors,
            )]

        try:
            proposals = self.glm_propose_fn(
                crossing_id=crossing_id,
                skill_type=skill_type,
                incumbent_dsl=incumbent_dsl,
                n_proposals=self.n_proposals,
            )
        except Exception as e:
            logger.error("GLM 方案生成异常: %s", e)
            return []

        # 验证每个方案
        valid_proposals = []
        for proposal in proposals:
            dsl_yaml = yaml.dump(
                proposal.dsl_template, default_flow_style=False, allow_unicode=True
            )
            vr = self.compiler.validate(dsl_yaml)
            proposal.compile_ok = vr.valid
            proposal.validation_errors = vr.errors
            if vr.valid:
                valid_proposals.append(proposal)
            else:
                logger.info(
                    "方案 %s 编译验证失败: %s",
                    proposal.proposal_id, vr.errors[:2],
                )

        return valid_proposals

    def _optimize_params(
        self,
        proposal: DSLStructureProposal,
        incumbent_score: float,
        skill_type: str,
    ) -> OptimizationResult:
        """对 DSL 模板执行参数优化。"""
        # 创建 SUMO 评估器工厂
        def evaluator_factory(python_code: str, s_type: str) -> float:
            return self._quick_evaluate(python_code, s_type)

        optimizer = DSLParamOptimizer(
            config=self.param_optimizer_config,
            evaluator_factory=evaluator_factory,
            compiler=self.compiler,
        )

        return optimizer.optimize(
            dsl_template=proposal.dsl_template,
            incumbent_score=incumbent_score,
        )

    def _quick_evaluate(self, python_code: str, skill_type: str) -> float:
        """快速评估函数。

        优先使用 SUMO 评估器。如果不可用，使用代码长度作为代理指标。
        """
        if self.sealed_evaluator is not None and self.cohort is not None:
            try:
                # 使用单种子快速评估
                report = self.sealed_evaluator.evaluate_candidate(
                    candidate_code=python_code,
                    skill_type=skill_type,
                    crossing_id="default",  # 由调用方确保
                    cohort=self.cohort,
                    seed=self.param_optimizer_config.seed,
                )
                if report.passed:
                    return report.score
                else:
                    # 评估未通过，给一个较高的惩罚分
                    return report.score + 100.0
            except Exception as e:
                logger.debug("SUMO 快速评估异常: %s", e)

        # 降级：使用代码长度作为代理
        return float(len(python_code))

    def _sealed_evaluate(
        self,
        crossing_id: str,
        skill_type: str,
        incumbent_code: str,
        candidate_code: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], float]:
        """执行 sealed paired evaluation。

        Returns
        -------
        (passed, sealed_report_dict, candidate_score)
        """
        if self.sealed_evaluator is None or self.cohort is None:
            logger.warning("无法执行 sealed evaluation: 缺少 evaluator 或 cohort")
            return False, None, float("inf")

        if not incumbent_code:
            logger.warning("无法执行 sealed evaluation: 缺少 incumbent code")
            return False, None, float("inf")

        try:
            report = self.sealed_evaluator.paired_evaluate(
                incumbent_code=incumbent_code,
                candidate_code=candidate_code,
                skill_type=skill_type,
                crossing_id=crossing_id,
                cohort=self.cohort,
            )
            candidate_score = report.candidate_metrics.get("score", float("inf"))
            return report.passed, report.to_dict(), candidate_score
        except Exception as e:
            logger.error("Sealed evaluation 异常: %s", e)
            return False, None, float("inf")


# ============================================================================
# 辅助工具
# ============================================================================

def extract_dsl_from_yaml(yaml_text: str) -> Optional[dict]:
    """从 YAML 文本解析 DSL。"""
    try:
        return yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return None


def dsl_to_yaml(dsl: dict) -> str:
    """将 DSL dict 转为 YAML 文本。"""
    return yaml.dump(dsl, default_flow_style=False, allow_unicode=True)


def compute_param_sensitivity(
    dsl_template: dict,
    param_name: str,
    n_points: int = 10,
    compiler: Optional[DslCompiler] = None,
) -> List[Tuple[float, bool]]:
    """计算单个参数的灵敏度（编译通过率）。

    Parameters
    ----------
    dsl_template : dict
        DSL 模板
    param_name : str
        要分析的参数名
    n_points : int
        采样点数
    compiler : DslCompiler, optional
        编译器实例

    Returns
    -------
    list[tuple[float, bool]]
        [(参数值, 编译是否通过), ...]
    """
    compiler = compiler or DslCompiler()
    param_range = DEFAULT_PARAM_RANGES.get(param_name)
    if param_range is None:
        return []

    lo, hi = param_range
    step = (hi - lo) / max(n_points - 1, 1)
    results = []

    for i in range(n_points):
        value = lo + step * i
        dsl = copy.deepcopy(dsl_template)
        if param_name in ("base", "queue_gain"):
            dsl.setdefault("cycle", {})[param_name] = value
        elif param_name in ("min_green", "max_green"):
            dsl.setdefault("allocation", {})[param_name] = value
        else:
            dsl.setdefault("parameters", {})[param_name] = value

        dsl_yaml = yaml.dump(dsl, default_flow_style=False, allow_unicode=True)
        result = compiler.compile(dsl_yaml)
        results.append((round(value, 4), result.success))

    return results


# ============================================================================
# 自测试
# ============================================================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("Test 1: _ensure_dsl_defaults")
    print("=" * 60)
    minimal = {"skill_type": "cycle", "parameters": {"w_queue": 1.0}}
    fixed = _ensure_dsl_defaults(minimal, "cycle")
    assert "cycle" in fixed
    assert "allocation" in fixed
    assert "guards" in fixed
    assert fixed["parameters"]["w_queue"] == 1.0
    print("  [OK] DSL 默认值填充正确")

    print()
    print("=" * 60)
    print("Test 2: DSLEvolutionPipeline (无 GLM, 无 SUMO)")
    print("=" * 60)

    cycle_template = {
        "skill_type": "cycle",
        "version_note": "测试",
        "features_used": ["queue", "waiting_time"],
        "parameters": {
            "w_queue": 1.0,
            "w_wait": 0.2,
        },
        "cycle": {"base": 80, "queue_gain": 0.5, "min": 40, "max": 180},
        "allocation": {"method": "softmax", "min_green": 10, "max_green": 60},
        "guards": {"all_phases_served": True, "max_cycle_jump": 20},
    }

    config = OptimizerConfig(method="random", n_trials=5, seed=42)
    pipeline = DSLEvolutionPipeline(
        param_optimizer_config=config,
        n_proposals=1,
        max_rounds=1,
    )
    result = pipeline.evolve_skill(
        crossing_id="test_crossing",
        skill_type="cycle",
        incumbent_dsl=cycle_template,
        incumbent_score=5000.0,
    )
    print(f"  passed: {result.passed}")
    print(f"  final_score: {result.final_score}")
    print(f"  n_proposals_tried: {result.n_proposals_tried}")
    print(f"  stages: {len(result.stage_results)}")
    assert result.n_proposals_tried >= 1
    print("  [OK] Pipeline 基本流程通过")

    print()
    print("=" * 60)
    print("Test 3: compute_param_sensitivity")
    print("=" * 60)
    sensitivity = compute_param_sensitivity(cycle_template, "w_queue", n_points=5)
    print(f"  采样点: {len(sensitivity)}")
    for val, ok in sensitivity:
        print(f"    w_queue={val:.2f} -> compile_ok={ok}")
    assert len(sensitivity) == 5
    print("  [OK] 参数灵敏度分析通过")

    print()
    print("=" * 60)
    print("Test 4: extract_dsl_from_yaml / dsl_to_yaml")
    print("=" * 60)
    yaml_text = dsl_to_yaml(cycle_template)
    parsed = extract_dsl_from_yaml(yaml_text)
    assert parsed is not None
    assert parsed["skill_type"] == "cycle"
    print("  [OK] YAML 序列化/反序列化通过")

    print()
    print("=" * 60)
    print("ALL TESTS PASSED")
