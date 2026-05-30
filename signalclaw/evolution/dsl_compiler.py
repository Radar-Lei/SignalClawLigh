"""DslCompiler - 将 YAML DSL 编译为可直接执行的 Python skill 代码。

LLM 输出结构化 YAML DSL（而非直接 Python），编译器生成兼容 runner/execution
框架的 Python skill 代码。这大幅降低了 AST 解析失败率，并确保输出的安全性
和确定性。

支持两种 skill 类型:
  - cycle: 周期规划器，输出 CyclePlan
  - phase: 相位微调器，输出 PhaseCommand
"""

from __future__ import annotations

import ast
import math
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from signalclaw.evolution.feature_mask import FeatureMask


# ============================================================================
# JSON Schema（用于 LLM 结构化输出约束）
# ============================================================================

CYCLE_DSL_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "CycleSkillDSL",
    "description": "交通信号周期规划器 DSL",
    "type": "object",
    "required": ["skill_type", "version_note", "parameters", "cycle", "allocation", "guards"],
    "additionalProperties": False,
    "properties": {
        "skill_type": {
            "type": "string",
            "const": "cycle",
            "description": "固定为 cycle",
        },
        "version_note": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            "description": "版本说明，简要描述改进思路",
        },
        "features_used": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "queue",
                    "waiting_time",
                    "predicted_arrival",
                    "downstream_queue",
                    "hunger_time",
                    "upstream_queue",
                    "downstream_spillback_risk",
                    "upstream_release_pressure",
                    "neighbor_queue",
                ],
            },
            "description": "使用的特征列表",
        },
        "parameters": {
            "type": "object",
            "required": ["w_queue"],
            "additionalProperties": {"type": "number"},
            "properties": {
                "w_queue": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "排队权重（核心，必须 > 0）",
                },
                "w_wait": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "等待时间权重",
                },
                "w_downstream": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "下游排队权重（通常 < 0）",
                },
                "w_hunger": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "饥饿惩罚权重",
                },
                "w_arrival": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "预测到达权重",
                },
                "w_spillback": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "溢出风险权重",
                },
            },
            "description": "评分函数中各特征权重",
        },
        "cycle": {
            "type": "object",
            "required": ["base"],
            "additionalProperties": False,
            "properties": {
                "base": {
                    "type": "number",
                    "minimum": 30.0,
                    "maximum": 300.0,
                    "description": "基础周期长度（秒）",
                },
                "queue_gain": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 2.0,
                    "description": "基于排队量的周期增益系数",
                },
                "min": {
                    "type": "number",
                    "minimum": 20.0,
                    "maximum": 120.0,
                    "description": "最小周期长度（秒）",
                },
                "max": {
                    "type": "number",
                    "minimum": 60.0,
                    "maximum": 300.0,
                    "description": "最大周期长度（秒）",
                },
            },
            "description": "周期长度自适应参数",
        },
        "allocation": {
            "type": "object",
            "required": ["method"],
            "additionalProperties": False,
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["softmax", "shifted_positive"],
                    "description": "绿灯时间分配方法",
                },
                "min_green": {
                    "type": "number",
                    "minimum": 5.0,
                    "maximum": 30.0,
                    "description": "最小绿灯时间（秒）",
                },
                "max_green": {
                    "type": "number",
                    "minimum": 20.0,
                    "maximum": 120.0,
                    "description": "最大绿灯时间（秒）",
                },
            },
            "description": "绿灯时间分配策略",
        },
        "guards": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "all_phases_served": {
                    "type": "boolean",
                    "description": "确保所有相位都被服务",
                },
                "max_cycle_jump": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 60,
                    "description": "周期长度单步最大跳变量（秒）",
                },
                "downstream_block_clip": {
                    "type": "boolean",
                    "description": "当下游溢出时裁剪绿灯时间",
                },
            },
            "description": "安全守卫条件",
        },
    },
}

PHASE_DSL_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "PhaseSkillDSL",
    "description": "交通信号相位微调器 DSL",
    "type": "object",
    "required": ["skill_type", "version_note", "parameters"],
    "additionalProperties": False,
    "properties": {
        "skill_type": {
            "type": "string",
            "const": "phase",
            "description": "固定为 phase",
        },
        "version_note": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            "description": "版本说明",
        },
        "parameters": {
            "type": "object",
            "required": ["w_queue"],
            "additionalProperties": {"type": "number"},
            "properties": {
                "w_queue": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "当前排队权重",
                },
                "w_waiting": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "等待时间权重",
                },
                "w_arrival": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "预测到达权重",
                },
                "w_hunger": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "饥饿惩罚权重",
                },
                "w_downstream": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "下游排队权重",
                },
                "w_switch": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 5.0,
                    "description": "切换惩罚权重（通常 < 0）",
                },
                "extend_threshold": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 20.0,
                    "description": "绿灯延长阈值",
                },
                "shorten_threshold": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 20.0,
                    "description": "绿灯缩短阈值",
                },
                "max_extend": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 15.0,
                    "description": "最大延长秒数",
                },
                "max_shorten": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 15.0,
                    "description": "最大缩短秒数",
                },
            },
            "description": "相位微调参数",
        },
    },
}


# ============================================================================
# 验证与编译结果
# ============================================================================

@dataclass
class DslValidationResult:
    """DSL 验证结果。"""
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    dsl_type: Optional[str] = None  # "cycle" | "phase"
    parsed: Optional[Dict[str, Any]] = None


@dataclass
class CompileResult:
    """编译结果。"""
    success: bool
    python_code: Optional[str] = None
    dsl_type: Optional[str] = None
    version_note: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ============================================================================
# 特征提取映射
# ============================================================================

_CYCLE_FEATURE_EXTRACTORS = {
    "queue": "phase_obs.queue",
    "waiting_time": "phase_obs.waiting_time",
    "predicted_arrival": "phase_obs.predicted_arrival",
    "downstream_queue": "avg_downstream",
    "hunger_time": "hunger_bonus",
    "upstream_queue": "avg_upstream",
    "downstream_spillback_risk": "ego.downstream_spillback_risk",
    "upstream_release_pressure": "ego.upstream_release_pressure",
    "neighbor_queue": "neighbor_total_queue",
}

_PHASE_FEATURE_EXTRACTORS = {
    "queue": "current_phase_obs.queue",
    "waiting": "current_phase_obs.waiting_time",
    "arrival": "current_phase_obs.predicted_arrival",
    "hunger": "hunger_time",
    "downstream": "downstream_total",
    "switch": "1.0",
}


# ============================================================================
# 分配方法代码生成器（纯字符串拼接，不使用 format）
# ============================================================================

def _gen_alloc_softmax() -> str:
    """生成 softmax 分配代码。"""
    return (
        "    # softmax 分配\n"
        "    _max_score = max(scores.values()) if scores else 0.0\n"
        "    exp_scores = {}\n"
        "    _temperature = 1.0\n"
        "    for _gp, _s in scores.items():\n"
        "        exp_scores[_gp] = math.exp((_s - _max_score) / _temperature)\n"
        "    total_score = sum(exp_scores.values())\n"
        "    weights = exp_scores"
    )


def _gen_alloc_shifted_positive() -> str:
    """生成 shifted_positive 分配代码。"""
    return (
        "    # shifted_positive 分配\n"
        "    min_score = min(scores.values()) if scores else 0.0\n"
        "    shifted = {gp: max(s - min_score + 1.0, 0.1) for gp, s in scores.items()}\n"
        "    total_score = sum(shifted.values())\n"
        "    weights = shifted"
    )


# ============================================================================
# DslCompiler
# ============================================================================

class DslCompiler:
    """YAML DSL -> Python skill 代码编译器。

    使用方式::

        compiler = DslCompiler()
        result = compiler.compile(yaml_text)
        if result.success:
            python_code = result.python_code  # 可直接 ast.parse / exec
    """

    def __init__(self, feature_mask: Optional[FeatureMask] = None):
        """初始化编译器。

        Parameters
        ----------
        feature_mask : FeatureMask, optional
            特征可用性门控。为 None 时使用默认配置（predicted_arrival=False）。
        """
        self.feature_mask = feature_mask or FeatureMask()

    # ------------------------------------------------------------------
    # 参数约束默认值
    # ------------------------------------------------------------------
    _DEFAULTS_CYCLE = {
        "w_queue": 1.0,
        "w_wait": 0.0,
        "w_downstream": 0.0,
        "w_hunger": 0.0,
        "w_arrival": 0.0,
        "w_spillback": 0.0,
    }
    _DEFAULTS_CYCLE_BLOCK = {
        "base": 80.0,
        "queue_gain": 0.5,
        "min": 40.0,
        "max": 180.0,
    }
    _DEFAULTS_ALLOCATION = {
        "method": "softmax",
        "min_green": 10.0,
        "max_green": 60.0,
    }
    _DEFAULTS_GUARDS = {
        "all_phases_served": True,
        "max_cycle_jump": 20,
        "downstream_block_clip": True,
    }

    _DEFAULTS_PHASE = {
        "w_queue": 1.0,
        "w_waiting": 0.0,
        "w_arrival": 0.0,
        "w_hunger": 0.0,
        "w_downstream": 0.0,
        "w_switch": 0.0,
        "extend_threshold": 3.0,
        "shorten_threshold": 1.0,
        "max_extend": 5.0,
        "max_shorten": 5.0,
    }

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------

    def validate(self, dsl_text: str) -> DslValidationResult:
        """验证 DSL 格式和参数范围。

        Parameters
        ----------
        dsl_text : str
            YAML 格式的 DSL 文本。

        Returns
        -------
        DslValidationResult
        """
        errors: List[str] = []
        warnings: List[str] = []

        # 1) YAML 解析
        try:
            parsed = yaml.safe_load(dsl_text)
        except yaml.YAMLError as exc:
            return DslValidationResult(
                valid=False,
                errors=[f"YAML 解析失败: {exc}"],
            )

        if not isinstance(parsed, dict):
            return DslValidationResult(
                valid=False,
                errors=["DSL 必须是一个 YAML mapping"],
            )

        # 2) skill_type 检查
        skill_type = parsed.get("skill_type")
        if skill_type not in ("cycle", "phase"):
            return DslValidationResult(
                valid=False,
                errors=[f"skill_type 必须是 'cycle' 或 'phase'，收到: {skill_type!r}"],
            )

        # 3) version_note 检查
        if "version_note" not in parsed or not parsed["version_note"]:
            errors.append("缺少 version_note 字段")

        # 4) parameters 检查
        params = parsed.get("parameters", {})
        if not isinstance(params, dict):
            errors.append("parameters 必须是一个 mapping")
        elif "w_queue" not in params:
            errors.append("parameters 中缺少必需的 w_queue 权重")

        # 5) Feature Mask 检查
        mask_result = self.feature_mask.check_dsl(parsed)
        if not mask_result.passed:
            for v in mask_result.violations:
                errors.append(f"Feature Mask 违规: {v.message}")

        # 6) 按类型做进一步验证
        if skill_type == "cycle":
            self._validate_cycle_dsl(parsed, errors, warnings)
        else:
            self._validate_phase_dsl(parsed, errors, warnings)

        return DslValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            dsl_type=skill_type,
            parsed=parsed,
        )

    # ------------------------------------------------------------------
    # compile (顶层入口)
    # ------------------------------------------------------------------

    def compile(self, dsl_text: str) -> CompileResult:
        """完整流程: 解析 YAML -> validate -> compile -> 返回 Python 代码。

        Parameters
        ----------
        dsl_text : str
            YAML 格式的 DSL 文本。

        Returns
        -------
        CompileResult
        """
        vr = self.validate(dsl_text)
        if not vr.valid:
            return CompileResult(
                success=False,
                errors=vr.errors,
                warnings=vr.warnings,
            )

        assert vr.parsed is not None
        dsl = vr.parsed

        try:
            if vr.dsl_type == "cycle":
                code = self.compile_cycle(dsl)
            else:
                code = self.compile_phase(dsl)
        except Exception as exc:
            return CompileResult(
                success=False,
                dsl_type=vr.dsl_type,
                errors=[f"编译失败: {exc}"],
                warnings=vr.warnings,
            )

        # 二次验证: 确保生成的代码可以被 ast.parse
        try:
            ast.parse(code)
        except SyntaxError as exc:
            return CompileResult(
                success=False,
                dsl_type=vr.dsl_type,
                errors=[f"生成的代码存在语法错误: {exc}"],
                warnings=vr.warnings,
            )

        return CompileResult(
            success=True,
            python_code=code,
            dsl_type=vr.dsl_type,
            version_note=dsl.get("version_note", ""),
            warnings=vr.warnings,
        )

    # ------------------------------------------------------------------
    # compile_cycle
    # ------------------------------------------------------------------

    def compile_cycle(self, dsl: Dict[str, Any]) -> str:
        """编译 cycle DSL 为 Python skill 代码。

        生成的代码符合 CyclePlannerSkill 协议 (plan(obs) -> CyclePlan)。
        """
        params = {**self._DEFAULTS_CYCLE, **dsl.get("parameters", {})}
        cycle_cfg = {**self._DEFAULTS_CYCLE_BLOCK, **dsl.get("cycle", {})}
        alloc_cfg = {**self._DEFAULTS_ALLOCATION, **dsl.get("allocation", {})}
        guard_cfg = {**self._DEFAULTS_GUARDS, **dsl.get("guards", {})}
        features = dsl.get("features_used", [])

        # 提取参数为本地变量
        w_queue = float(params["w_queue"])
        w_wait = float(params.get("w_wait", 0.0))
        w_downstream = float(params.get("w_downstream", 0.0))
        w_hunger = float(params.get("w_hunger", 0.0))
        w_arrival = float(params.get("w_arrival", 0.0))
        w_spillback = float(params.get("w_spillback", 0.0))

        base_cycle = float(cycle_cfg["base"])
        queue_gain = float(cycle_cfg.get("queue_gain", 0.5))
        cycle_min = float(cycle_cfg.get("min", 40.0))
        cycle_max = float(cycle_cfg.get("max", 180.0))

        method = alloc_cfg["method"]
        min_green = float(alloc_cfg.get("min_green", 10.0))
        max_green = float(alloc_cfg.get("max_green", 60.0))

        all_phases_served = bool(guard_cfg.get("all_phases_served", True))
        max_cycle_jump = float(guard_cfg.get("max_cycle_jump", 20))
        downstream_block_clip = bool(guard_cfg.get("downstream_block_clip", True))

        # 构建各代码段
        feature_init = self._build_cycle_feature_init(features)
        loop_body_extra = self._build_cycle_loop_body(features)
        score_terms = self._build_cycle_score_terms(
            features, w_queue, w_wait, w_downstream, w_hunger, w_arrival, w_spillback,
        )
        alloc_code = _gen_alloc_softmax() if method == "softmax" else _gen_alloc_shifted_positive()
        guard_lines = self._build_cycle_guards(
            all_phases_served, max_cycle_jump, downstream_block_clip, min_green,
        )

        # 构造完整代码（不使用 .format()，直接用字符串拼接和 f-string）
        lines = []

        # 文件头和模块变量
        lines.append('"""Cycle planner skill (auto-generated from DSL)."""')
        lines.append("import math")
        lines.append("from typing import Dict, List, Optional")
        lines.append("from collections import deque")
        lines.append("from signalclaw.core.state import NetworkObservation, CyclePlan")
        lines.append("")
        lines.append(f"_min_green = {min_green!r}")
        lines.append(f"_max_green = {max_green!r}")
        lines.append(f"_base_cycle = {base_cycle!r}")
        lines.append(f"_queue_gain = {queue_gain!r}")
        lines.append(f"_cycle_min = {cycle_min!r}")
        lines.append(f"_cycle_max = {cycle_max!r}")
        lines.append(f"_max_cycle_jump = {max_cycle_jump!r}")
        lines.append("")
        lines.append("_pressure_history: Dict[int, deque] = {}")
        lines.append("_last_green_time: Dict[int, float] = {}")
        lines.append(f"_prev_cycle_length: float = {base_cycle!r}")
        lines.append("")
        lines.append("")

        # _get_history 辅助函数
        lines.append("def _get_history(phase_id: int) -> deque:")
        lines.append("    if phase_id not in _pressure_history:")
        lines.append("        _pressure_history[phase_id] = deque(maxlen=5)")
        lines.append("    return _pressure_history[phase_id]")
        lines.append("")
        lines.append("")

        # plan() 函数
        lines.append('def plan(obs: "NetworkObservation") -> "CyclePlan":')
        lines.append("    global _prev_cycle_length")
        lines.append("    ego = obs.ego")
        lines.append("    green_phases = sorted(ego.phases.keys())")
        lines.append("    if not green_phases:")
        lines.append("        return CyclePlan(cycle_length=_base_cycle, green_times={}, phase_order=[])")
        lines.append("")

        # 特征预处理
        lines.append("    # --- 特征预处理 ---")
        for fl in feature_init.split("\n"):
            lines.append(fl)
        lines.append("")

        # 评分计算
        lines.append("    # --- 评分计算 ---")
        lines.append("    scores = {}")
        lines.append("    for gp in green_phases:")
        lines.append("        phase_obs = ego.phases.get(gp)")
        lines.append("        if phase_obs is None:")
        lines.append("            scores[gp] = 0.0")
        lines.append("            continue")
        # for 循环体内依赖 gp 的特征预处理（如 hunger_time）
        for lb in loop_body_extra:
            lines.append(lb)
        lines.append("        score = (")
        for i, term in enumerate(score_terms):
            if i == 0:
                lines.append(f"            {term}")
            else:
                # 加号对齐
                if term.startswith("-"):
                    lines.append(f"            {term}")
                else:
                    lines.append(f"            + {term}")
        lines.append("        )")
        lines.append("        scores[gp] = score")
        lines.append("        _get_history(gp).append(score)")
        lines.append("")

        # 周期长度自适应
        lines.append("    # --- 周期长度自适应 ---")
        lines.append("    total_queue = sum(p.queue for p in ego.phases.values())")
        lines.append("    cycle_length = _base_cycle + _queue_gain * total_queue")
        lines.append("    cycle_length = max(_cycle_min, min(_cycle_max, cycle_length))")
        lines.append("")

        # 绿灯分配
        lines.append("    # --- 绿灯时间分配 ---")
        lines.append("    min_green_val = _min_green")
        lines.append("    max_green_val = _max_green")
        for al in alloc_code.split("\n"):
            lines.append(al)
        lines.append("")
        lines.append("    green_times = {}")
        lines.append("    for gp in green_phases:")
        lines.append("        if total_score > 0:")
        lines.append("            gt = cycle_length * (weights[gp] / total_score)")
        lines.append("        else:")
        lines.append("            gt = cycle_length / len(green_phases)")
        lines.append("        green_times[gp] = max(min_green_val, min(max_green_val, gt))")
        lines.append("")

        # Guards
        lines.append("    # --- Guards ---")
        if guard_lines.strip():
            for gl in guard_lines.split("\n"):
                lines.append(gl)
        lines.append("")

        # 构造 CyclePlan
        lines.append("    # --- 构造 CyclePlan ---")
        lines.append("    plan_cycle_length = sum(green_times.values())")
        if max_cycle_jump > 0:
            lines.append("    # cycle jump guard")
            lines.append("    _delta = plan_cycle_length - _prev_cycle_length")
            lines.append(f"    if abs(_delta) > {max_cycle_jump!r}:")
            lines.append(f"        plan_cycle_length = _prev_cycle_length + {max_cycle_jump!r} * (1 if _delta > 0 else -1)")
        lines.append("    _prev_cycle_length = plan_cycle_length")
        lines.append("")
        lines.append("    return CyclePlan(")
        lines.append("        cycle_length=plan_cycle_length,")
        lines.append("        green_times=green_times,")
        lines.append("        phase_order=green_phases,")
        lines.append("    )")
        lines.append("")
        lines.append("")

        # _reset 函数
        lines.append("def _reset():")
        lines.append("    global _prev_cycle_length")
        lines.append("    _pressure_history.clear()")
        lines.append("    _last_green_time.clear()")
        lines.append("    _prev_cycle_length = _base_cycle")
        lines.append("")

        # 清理冗余行（多余的空行合并）
        code = "\n".join(lines)
        # 移除连续三个以上的空行
        import re
        code = re.sub(r'\n{4,}', '\n\n\n', code)
        return code

    # ------------------------------------------------------------------
    # compile_phase
    # ------------------------------------------------------------------

    def compile_phase(self, dsl: Dict[str, Any]) -> str:
        """编译 phase DSL 为 Python skill 代码。

        生成的代码符合 PhaseMicroSkill 协议 (decide(obs, plan) -> PhaseCommand)。
        """
        params = {**self._DEFAULTS_PHASE, **dsl.get("parameters", {})}

        w_queue = float(params["w_queue"])
        w_waiting = float(params.get("w_waiting", 0.0))
        w_arrival = float(params.get("w_arrival", 0.0))
        w_hunger = float(params.get("w_hunger", 0.0))
        w_downstream = float(params.get("w_downstream", 0.0))
        w_switch = float(params.get("w_switch", 0.0))

        extend_threshold = float(params.get("extend_threshold", 3.0))
        shorten_threshold = float(params.get("shorten_threshold", 1.0))
        max_extend = float(params.get("max_extend", 5.0))
        max_shorten = float(params.get("max_shorten", 5.0))

        score_terms = self._build_phase_score_terms(
            w_queue, w_waiting, w_arrival, w_hunger, w_downstream, w_switch,
        )

        lines = []

        # 文件头
        lines.append('"""Phase micro-adjuster skill (auto-generated from DSL)."""')
        lines.append("import math")
        lines.append("from typing import Dict, List, Optional")
        lines.append("from signalclaw.core.state import NetworkObservation, CyclePlan, PhaseCommand")
        lines.append("")
        lines.append(f"_extend_threshold = {extend_threshold!r}")
        lines.append(f"_shorten_threshold = {shorten_threshold!r}")
        lines.append(f"_max_extend = {max_extend!r}")
        lines.append(f"_max_shorten = {max_shorten!r}")
        lines.append("")
        lines.append("_phase_index: Dict[str, int] = {}")
        lines.append("_phase_remaining: Dict[str, float] = {}")
        lines.append("_last_phase_served: Dict[str, float] = {}")
        lines.append("")
        lines.append("")

        # decide() 函数
        lines.append('def decide(obs: "NetworkObservation", plan: "CyclePlan") -> "PhaseCommand":')
        lines.append("    ego = obs.ego")
        lines.append("    tls_id = ego.crossing_id")
        lines.append("    phase_order = plan.phase_order")
        lines.append("")
        lines.append("    if not phase_order:")
        lines.append("        return PhaseCommand(")
        lines.append("            action=\"hold\",")
        lines.append("            next_phase_id=ego.current_phase_id,")
        lines.append("            duration=5.0,")
        lines.append("            reason_code=\"no_phases\",")
        lines.append("        )")
        lines.append("")
        lines.append("    # --- 相位追踪 ---")
        lines.append("    if tls_id not in _phase_index:")
        lines.append("        _phase_index[tls_id] = 0")
        lines.append("        _phase_remaining[tls_id] = plan.green_times.get(phase_order[0], 15.0)")
        lines.append("        first_phase = phase_order[0]")
        lines.append("        return PhaseCommand(")
        lines.append("            action=\"switch\",")
        lines.append("            next_phase_id=first_phase,")
        lines.append("            duration=plan.green_times.get(first_phase, 15.0),")
        lines.append("            reason_code=\"new_plan\",")
        lines.append("        )")
        lines.append("")
        lines.append("    current_idx = _phase_index[tls_id]")
        lines.append("    remaining = _phase_remaining[tls_id]")
        lines.append("")
        lines.append("    # --- 相位切换 ---")
        lines.append("    if remaining <= 0:")
        lines.append("        next_idx = (current_idx + 1) % len(phase_order)")
        lines.append("        next_phase = phase_order[next_idx]")
        lines.append("        _phase_index[tls_id] = next_idx")
        lines.append("        _phase_remaining[tls_id] = plan.green_times.get(next_phase, 15.0)")
        lines.append("        return PhaseCommand(")
        lines.append("            action=\"switch\",")
        lines.append("            next_phase_id=next_phase,")
        lines.append("            duration=_phase_remaining[tls_id],")
        lines.append("            reason_code=\"phase_end\",")
        lines.append("        )")
        lines.append("")
        lines.append("    # --- 微调评分 ---")
        lines.append("    current_phase = phase_order[current_idx]")
        lines.append("    current_phase_obs = ego.phases.get(current_phase)")
        lines.append("    downstream_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0")
        lines.append("    last_served = _last_phase_served.get(tls_id, 0.0)")
        lines.append("    hunger_time = obs.timestamp - last_served")
        lines.append("")
        lines.append("    if current_phase_obs is not None and remaining <= 10.0:")
        lines.append("        current_score = (")
        for i, term in enumerate(score_terms):
            if i == 0:
                lines.append(f"                {term}")
            else:
                if term.startswith("-"):
                    lines.append(f"                {term}")
                else:
                    lines.append(f"                + {term}")
        lines.append("        )")
        lines.append("")
        lines.append("        # 延长判定")
        lines.append("        if current_score > _extend_threshold and downstream_total < 10:")
        lines.append("            extend_amount = min(_max_extend, 5.0)")
        lines.append("            _phase_remaining[tls_id] = remaining + extend_amount")
        lines.append("            _phase_remaining[tls_id] -= 5.0")
        lines.append("            _last_phase_served[tls_id] = obs.timestamp")
        lines.append("            return PhaseCommand(")
        lines.append("                action=\"extend\",")
        lines.append("                next_phase_id=current_phase,")
        lines.append("                duration=_phase_remaining[tls_id] + 5.0,")
        lines.append("                reason_code=\"extend_high_demand\",")
        lines.append("            )")
        lines.append("")
        lines.append("        # 缩短判定")
        lines.append("        if current_phase_obs is not None and current_phase_obs.queue < _shorten_threshold and remaining > 5.0:")
        lines.append("            _phase_remaining[tls_id] = 0")
        lines.append("            next_idx = (current_idx + 1) % len(phase_order)")
        lines.append("            next_phase = phase_order[next_idx]")
        lines.append("            _phase_index[tls_id] = next_idx")
        lines.append("            _phase_remaining[tls_id] = plan.green_times.get(next_phase, 15.0)")
        lines.append("            return PhaseCommand(")
        lines.append("                action=\"switch\",")
        lines.append("                next_phase_id=next_phase,")
        lines.append("                duration=_phase_remaining[tls_id],")
        lines.append("                reason_code=\"early_switch_empty\",")
        lines.append("            )")
        lines.append("")
        lines.append("    # --- 默认 hold ---")
        lines.append("    _phase_remaining[tls_id] -= 5.0")
        lines.append("    _last_phase_served[tls_id] = obs.timestamp")
        lines.append("    return PhaseCommand(")
        lines.append("        action=\"hold\",")
        lines.append("        next_phase_id=current_phase,")
        lines.append("        duration=5.0,")
        lines.append("        reason_code=\"continuing\",")
        lines.append("    )")
        lines.append("")
        lines.append("")
        lines.append("def _reset():")
        lines.append("    _phase_index.clear()")
        lines.append("    _phase_remaining.clear()")
        lines.append("    _last_phase_served.clear()")
        lines.append("")

        return "\n".join(lines)

    # ==================================================================
    # 内部辅助方法
    # ==================================================================

    def _validate_cycle_dsl(
        self,
        dsl: Dict[str, Any],
        errors: List[str],
        warnings: List[str],
    ) -> None:
        """验证 cycle 类型 DSL。"""
        cycle_cfg = dsl.get("cycle", {})
        if not isinstance(cycle_cfg, dict):
            errors.append("cycle 必须是一个 mapping")
            return

        base = cycle_cfg.get("base")
        if base is not None:
            if not (30.0 <= float(base) <= 300.0):
                errors.append(f"cycle.base 必须在 [30, 300] 范围内，收到: {base}")

        cmin = cycle_cfg.get("min")
        cmax = cycle_cfg.get("max")
        if cmin is not None and cmax is not None:
            if float(cmin) >= float(cmax):
                errors.append(f"cycle.min ({cmin}) 必须小于 cycle.max ({cmax})")

        alloc = dsl.get("allocation", {})
        if isinstance(alloc, dict):
            amin = alloc.get("min_green")
            amax = alloc.get("max_green")
            if amin is not None and amax is not None:
                if float(amin) >= float(amax):
                    errors.append(
                        f"allocation.min_green ({amin}) 必须 < allocation.max_green ({amax})"
                    )
            method = alloc.get("method")
            if method is not None and method not in ("softmax", "shifted_positive"):
                errors.append(f"allocation.method 必须是 'softmax' 或 'shifted_positive'，收到: {method!r}")

        # 权重范围警告
        params = dsl.get("parameters", {})
        if isinstance(params, dict):
            for k, v in params.items():
                if not (-5.0 <= float(v) <= 5.0):
                    warnings.append(f"parameters.{k} = {v} 超出建议范围 [-5, 5]")

    def _validate_phase_dsl(
        self,
        dsl: Dict[str, Any],
        errors: List[str],
        warnings: List[str],
    ) -> None:
        """验证 phase 类型 DSL。"""
        params = dsl.get("parameters", {})
        if isinstance(params, dict):
            for k, v in params.items():
                if not (-5.0 <= float(v) <= 5.0):
                    warnings.append(f"parameters.{k} = {v} 超出建议范围 [-5, 5]")

    def _build_cycle_feature_init(self, features: List[str]) -> str:
        """根据使用的特征构建预处理代码行（for 循环外部的特征）。"""
        lines: List[str] = []

        if "downstream_queue" in features:
            lines.append("    downstream_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0")
            lines.append("    n_downstream = max(len(ego.downstream_queue), 1)")
            lines.append("    avg_downstream = downstream_total / n_downstream")

        if "upstream_queue" in features:
            lines.append("    upstream_total = sum(ego.upstream_queue.values()) if ego.upstream_queue else 0.0")
            lines.append("    n_upstream = max(len(ego.upstream_queue), 1)")
            lines.append("    avg_upstream = upstream_total / n_upstream")

        if "neighbor_queue" in features:
            lines.append("    neighbor_total_queue = 0.0")
            lines.append("    for _nid, _nobs in obs.neighbors.items():")
            lines.append("        for _po in _nobs.phases.values():")
            lines.append("            neighbor_total_queue += _po.queue")

        # downstream_spillback_risk, upstream_release_pressure 直接用 ego.xxx
        # hunger_time 在 for 循环体内部计算（依赖 gp 变量）

        if not lines:
            lines.append("    # no additional feature preprocessing needed")

        return "\n".join(lines)

    @staticmethod
    def _build_cycle_loop_body(features: List[str]) -> List[str]:
        """构建 for 循环体内部的额外代码（依赖 gp 的特征）。"""
        loop_lines: List[str] = []
        if "hunger_time" in features:
            loop_lines.append("        last_served = _last_green_time.get(gp, 0)")
            loop_lines.append("        raw_hunger = obs.timestamp - last_served")
            loop_lines.append("        hunger_bonus = min(raw_hunger * 0.5, 15.0)")
        return loop_lines

    def _build_cycle_score_terms(
        self,
        features: List[str],
        w_queue: float,
        w_wait: float,
        w_downstream: float,
        w_hunger: float,
        w_arrival: float,
        w_spillback: float,
    ) -> List[str]:
        """构建评分表达式中的各项，返回列表。"""
        terms: List[str] = []
        terms.append(f"{w_queue:.6g} * phase_obs.queue")

        if w_wait != 0.0 and "waiting_time" in features:
            terms.append(f"{w_wait:.6g} * phase_obs.waiting_time")

        if w_downstream != 0.0 and "downstream_queue" in features:
            terms.append(f"{w_downstream:.6g} * avg_downstream")

        if w_hunger != 0.0 and "hunger_time" in features:
            terms.append(f"{w_hunger:.6g} * hunger_bonus")

        if w_arrival != 0.0 and "predicted_arrival" in features:
            terms.append(f"{w_arrival:.6g} * phase_obs.predicted_arrival")

        if w_spillback != 0.0 and "downstream_spillback_risk" in features:
            terms.append(f"{w_spillback:.6g} * ego.downstream_spillback_risk")

        if not terms:
            terms.append("0.0")

        return terms

    def _build_phase_score_terms(
        self,
        w_queue: float,
        w_waiting: float,
        w_arrival: float,
        w_hunger: float,
        w_downstream: float,
        w_switch: float,
    ) -> List[str]:
        """构建 phase skill 的评分项，返回列表。"""
        terms: List[str] = []

        if w_queue != 0.0:
            terms.append(f"{w_queue:.6g} * current_phase_obs.queue")
        if w_waiting != 0.0:
            terms.append(f"{w_waiting:.6g} * current_phase_obs.waiting_time")
        if w_arrival != 0.0:
            terms.append(f"{w_arrival:.6g} * current_phase_obs.predicted_arrival")
        if w_hunger != 0.0:
            terms.append(f"{w_hunger:.6g} * hunger_time")
        if w_downstream != 0.0:
            terms.append(f"{w_downstream:.6g} * downstream_total")
        if w_switch != 0.0:
            terms.append(f"{w_switch:.6g} * 1.0")

        if not terms:
            terms.append("0.0")

        return terms

    def _build_cycle_guards(
        self,
        all_phases_served: bool,
        max_cycle_jump: float,
        downstream_block_clip: bool,
        min_green: float,
    ) -> str:
        """构建守卫条件代码。"""
        parts: List[str] = []

        if all_phases_served:
            parts.append("    # Guard: 确保所有相位都被分配了绿灯时间")
            parts.append("    for _gp in green_phases:")
            parts.append("        if _gp not in green_times:")
            parts.append(f"            green_times[_gp] = {min_green!r}")

        if downstream_block_clip:
            parts.append("    # Guard: 下游溢出时裁剪绿灯时间")
            parts.append("    for _gp in green_phases:")
            parts.append("        _downstream_sum = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0")
            parts.append("        if _downstream_sum > 30.0:")
            parts.append("            green_times[_gp] = max(green_times[_gp] * 0.7, min_green_val)")

        return "\n".join(parts)


# ============================================================================
# DslSchema 便捷类
# ============================================================================

class DslSchema:
    """提供 JSON schema 用于 LLM 结构化输出约束的便捷访问。"""

    CYCLE_DSL_SCHEMA = CYCLE_DSL_SCHEMA
    PHASE_DSL_SCHEMA = PHASE_DSL_SCHEMA

    @staticmethod
    def get_schema(skill_type: str) -> Dict[str, Any]:
        """根据 skill_type 返回对应的 JSON schema。"""
        if skill_type == "cycle":
            return CYCLE_DSL_SCHEMA
        elif skill_type == "phase":
            return PHASE_DSL_SCHEMA
        else:
            raise ValueError(f"未知的 skill_type: {skill_type!r}")

    @staticmethod
    def get_example(skill_type: str) -> str:
        """返回对应类型的 DSL 示例 YAML。"""
        if skill_type == "cycle":
            return _CYCLE_EXAMPLE_YAML
        elif skill_type == "phase":
            return _PHASE_EXAMPLE_YAML
        else:
            raise ValueError(f"未知的 skill_type: {skill_type!r}")


_CYCLE_EXAMPLE_YAML = """\
skill_type: cycle
version_note: "基础多因子评分周期规划器"
features_used:
  - queue
  - waiting_time
  - downstream_queue
  - hunger_time
parameters:
  w_queue: 1.0
  w_wait: 0.2
  w_downstream: -0.8
  w_hunger: 0.6
cycle:
  base: 80
  queue_gain: 0.5
  min: 40
  max: 180
allocation:
  method: softmax
  min_green: 10
  max_green: 60
guards:
  all_phases_served: true
  max_cycle_jump: 20
  downstream_block_clip: true
"""

_PHASE_EXAMPLE_YAML = """\
skill_type: phase
version_note: "基础相位微调器（不含 predicted_arrival）"
parameters:
  w_queue: 1.0
  w_waiting: 0.25
  w_hunger: 0.6
  w_downstream: -1.2
  w_switch: -0.3
  extend_threshold: 3.0
  shorten_threshold: 1.0
  max_extend: 5.0
  max_shorten: 5.0
"""


# ============================================================================
# 自测试
# ============================================================================

if __name__ == "__main__":
    import sys
    import re as _re

    compiler = DslCompiler()
    all_ok = True

    # --- Test 1: Cycle DSL (softmax) ---
    print("=" * 60)
    print("Test 1: Compile cycle DSL (softmax)")
    print("=" * 60)
    cycle_yaml = _CYCLE_EXAMPLE_YAML
    result = compiler.compile(cycle_yaml)
    if result.success:
        print(f"  [OK] 编译成功, type={result.dsl_type}")
        print(f"  version_note: {result.version_note}")
        try:
            tree = ast.parse(result.python_code)
            print(f"  [OK] ast.parse 通过, {len(tree.body)} 个顶级节点")
        except SyntaxError as e:
            print(f"  [FAIL] ast.parse 失败: {e}")
            all_ok = False
    else:
        print(f"  [FAIL] 编译失败: {result.errors}")
        all_ok = False

    # --- Test 2: Phase DSL ---
    print()
    print("=" * 60)
    print("Test 2: Compile phase DSL")
    print("=" * 60)
    phase_yaml = _PHASE_EXAMPLE_YAML
    result = compiler.compile(phase_yaml)
    if result.success:
        print(f"  [OK] 编译成功, type={result.dsl_type}")
        print(f"  version_note: {result.version_note}")
        try:
            tree = ast.parse(result.python_code)
            print(f"  [OK] ast.parse 通过, {len(tree.body)} 个顶级节点")
        except SyntaxError as e:
            print(f"  [FAIL] ast.parse 失败: {e}")
            all_ok = False
    else:
        print(f"  [FAIL] 编译失败: {result.errors}")
        all_ok = False

    # --- Test 3: 验证无效 DSL ---
    print()
    print("=" * 60)
    print("Test 3: Validate invalid DSL")
    print("=" * 60)
    bad_yaml = "skill_type: unknown\nversion_note: test"
    vr = compiler.validate(bad_yaml)
    if not vr.valid:
        print(f"  [OK] 正确拒绝无效 DSL: {vr.errors}")
    else:
        print(f"  [FAIL] 不应通过验证")
        all_ok = False

    # --- Test 4: 验证缺少必要字段 ---
    print()
    print("=" * 60)
    print("Test 4: Validate missing fields")
    print("=" * 60)
    missing_yaml = "skill_type: cycle\nversion_note: test"
    vr = compiler.validate(missing_yaml)
    if not vr.valid:
        print(f"  [OK] 正确拒绝缺少 parameters 的 DSL: {vr.errors}")
    else:
        print(f"  [FAIL] 不应通过验证")
        all_ok = False

    # --- Test 5: shifted_positive 分配方法 ---
    print()
    print("=" * 60)
    print("Test 5: Compile cycle DSL with shifted_positive allocation")
    print("=" * 60)
    shifted_yaml = """\
skill_type: cycle
version_note: "shifted_positive 分配"
features_used:
  - queue
parameters:
  w_queue: 1.5
cycle:
  base: 90
  queue_gain: 0.3
  min: 50
  max: 200
allocation:
  method: shifted_positive
  min_green: 12
  max_green: 55
guards:
  all_phases_served: true
  max_cycle_jump: 15
  downstream_block_clip: false
"""
    result = compiler.compile(shifted_yaml)
    if result.success:
        print(f"  [OK] 编译成功, type={result.dsl_type}")
        try:
            tree = ast.parse(result.python_code)
            print(f"  [OK] ast.parse 通过, {len(tree.body)} 个顶级节点")
        except SyntaxError as e:
            print(f"  [FAIL] ast.parse 失败: {e}")
            all_ok = False
    else:
        print(f"  [FAIL] 编译失败: {result.errors}")
        all_ok = False

    # --- Test 6: DslSchema 便捷接口 ---
    print()
    print("=" * 60)
    print("Test 6: DslSchema convenience methods")
    print("=" * 60)
    schema = DslSchema.get_schema("cycle")
    print(f"  [OK] cycle schema title: {schema.get('title')}")
    schema = DslSchema.get_schema("phase")
    print(f"  [OK] phase schema title: {schema.get('title')}")
    example = DslSchema.get_example("cycle")
    print(f"  [OK] cycle example length: {len(example)} chars")

    # --- 汇总 ---
    print()
    print("=" * 60)
    if all_ok:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)
