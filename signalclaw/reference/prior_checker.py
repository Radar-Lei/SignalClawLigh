"""Prior Consistency Checker - 检查候选 Skill 是否违背真实先验。

不做反事实评估，只做静态特征检查：
  1. 周期时长是否在合理范围
  2. 绿灯时长是否在合理范围
  3. 微调幅度是否合理
  4. 是否有可能产生极端值

这是快速检查，不跑 SUMO。真正的效果评估在 SUMO 中进行。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from signalclaw.reference.profile_schema import SQLReferenceProfile


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class PriorCheckResult:
    """先验一致性检查结果。"""

    passed: bool
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    score: float = 0.0  # 0.0 ~ 1.0, 越高越符合先验


# ---------------------------------------------------------------------------
# PriorConsistencyChecker
# ---------------------------------------------------------------------------


class PriorConsistencyChecker:
    """检查候选 Skill 是否违背 SQL 真实先验。

    静态特征检查，不需要运行 SUMO。
    检查层级:
      - 代码静态分析: 检查代码中硬编码的数值是否超出先验范围
      - 输出结构检查: 对已生成的 CyclePlan / PhaseCommand 进行合理性验证
    """

    def __init__(self, profile: SQLReferenceProfile):
        self.profile = profile

    # ==================================================================
    # 代码级静态检查
    # ==================================================================

    def check(self, skill_code: str, skill_type: str) -> PriorCheckResult:
        """检查候选 Skill 代码是否可能违背真实先验。

        通过静态分析代码中的数值字面量，检查是否存在明显不合理的值。

        Parameters
        ----------
        skill_code : str
            候选 Skill 的 Python 代码
        skill_type : str
            "cycle" 或 "phase"

        Returns
        -------
        PriorCheckResult
        """
        violations: List[str] = []
        warnings: List[str] = []

        if skill_type == "cycle":
            self._check_cycle_code(skill_code, violations, warnings)
        elif skill_type == "phase":
            self._check_phase_code(skill_code, violations, warnings)
        else:
            warnings.append(f"未知的 skill_type: {skill_type}")

        # 计算一致性分数
        score = self._compute_score(violations, warnings)

        return PriorCheckResult(
            passed=len(violations) == 0,
            violations=violations,
            warnings=warnings,
            score=score,
        )

    def _check_cycle_code(
        self,
        code: str,
        violations: List[str],
        warnings: List[str],
    ) -> None:
        """检查 Cycle Skill 代码中的静态特征。

        注意：数值字面量检查仅作为 warning，不作为 hard violation。
        因为代码中的数值可能有多种用途（如权重、阈值等），不一定都是周期/绿灯参数。
        """
        cp = self.profile.cycle_duration_prior
        pg = self.profile.phase_green_prior

        # 提取代码中的数值字面量
        numbers = _extract_numeric_literals(code)

        # 检查是否有可能产生极端周期时长（仅 warning）
        for num in numbers:
            if num > cp.max_recommended * 2:
                warnings.append(
                    f"代码中存在可能产生极端周期时长的数值: {num:.1f}s"
                    f" (先验上限: {cp.max_recommended:.0f}s)"
                )
                break

        # 不再对过小的数值做 violation 检查（改为 warning）
        # 因为 min_green=10.0 等合法参数也会被提取出来

    def _check_phase_code(
        self,
        code: str,
        violations: List[str],
        warnings: List[str],
    ) -> None:
        """检查 Phase Skill 代码中的静态特征。

        数值字面量检查仅作为 warning，不作为 hard violation。
        """
        ma = self.profile.micro_adjustment_prior

        numbers = _extract_numeric_literals(code)

        # 检查 extend/shorten 幅度（仅 warning）
        for num in numbers:
            if num > ma.max_extend_recommended * 3:
                warnings.append(
                    f"代码中存在可能产生过大延长幅度的数值: {num:.1f}s"
                    f" (先验上限: {ma.max_extend_recommended:.0f}s)"
                )
                break

    # ==================================================================
    # 输出结构检查
    # ==================================================================

    def check_cycle_plan(self, plan_dict: dict) -> PriorCheckResult:
        """检查一个 CyclePlan 的输出是否合理。

        Parameters
        ----------
        plan_dict : dict
            CyclePlan 的字典表示，应包含:
              - cycle_length: float
              - green_times: Dict[int, float]
              - phase_order: List[int]

        Returns
        -------
        PriorCheckResult
        """
        violations: List[str] = []
        warnings: List[str] = []

        cp = self.profile.cycle_duration_prior
        pg = self.profile.phase_green_prior

        cycle_length = plan_dict.get("cycle_length", 0)
        green_times = plan_dict.get("green_times", {})
        phase_order = plan_dict.get("phase_order", [])

        # ---- 检查周期时长 ----
        if not _is_valid_number(cycle_length):
            violations.append(f"cycle_length={cycle_length} 不是有效数值")
        elif cycle_length < cp.min_recommended:
            violations.append(
                f"cycle_length={cycle_length:.1f}s"
                f" < 先验下限 {cp.min_recommended:.0f}s"
            )
        elif cycle_length > cp.max_recommended:
            violations.append(
                f"cycle_length={cycle_length:.1f}s"
                f" > 先验上限 {cp.max_recommended:.0f}s"
            )
        elif cycle_length < cp.p25:
            warnings.append(
                f"cycle_length={cycle_length:.1f}s 低于先验 P25={cp.p25:.0f}s"
            )
        elif cycle_length > cp.p75:
            warnings.append(
                f"cycle_length={cycle_length:.1f}s 高于先验 P75={cp.p75:.0f}s"
            )

        # ---- 检查各相位绿灯时间 ----
        for phase_id, green_time in green_times.items():
            if not _is_valid_number(green_time):
                violations.append(
                    f"phase {phase_id} green_time={green_time} 不是有效数值"
                )
                continue

            if green_time < pg.min_green_recommended:
                violations.append(
                    f"phase {phase_id} green_time={green_time:.1f}s"
                    f" < 先验下限 {pg.min_green_recommended:.0f}s"
                )
            elif green_time > pg.max_green_recommended:
                violations.append(
                    f"phase {phase_id} green_time={green_time:.1f}s"
                    f" > 先验上限 {pg.max_green_recommended:.0f}s"
                )

        # ---- 检查相位覆盖 ----
        for pid in phase_order:
            if pid not in green_times:
                violations.append(
                    f"phase {pid} 在 phase_order 中但不在 green_times 中"
                )

        # ---- 检查绿灯时间总和与周期时长一致性 ----
        total_green = sum(
            gt for gt in green_times.values()
            if _is_valid_number(gt)
        )
        if _is_valid_number(cycle_length) and cycle_length > 0:
            ratio = total_green / cycle_length
            if ratio < 0.7:
                warnings.append(
                    f"绿灯总时间 {total_green:.1f}s 仅占周期 {cycle_length:.1f}s"
                    f" 的 {ratio:.0%}，可能存在过多损失时间"
                )
            elif ratio > 1.05:
                warnings.append(
                    f"绿灯总时间 {total_green:.1f}s 超过周期 {cycle_length:.1f}s"
                )

        # ---- 检查相位均衡度 ----
        valid_greens = [gt for gt in green_times.values() if _is_valid_number(gt) and gt > 0]
        if len(valid_greens) > 1:
            mean_g = sum(valid_greens) / len(valid_greens)
            if mean_g > 0:
                variance = sum((g - mean_g) ** 2 for g in valid_greens) / len(valid_greens)
                cv = (variance ** 0.5) / mean_g
                if cv > 1.5:
                    warnings.append(
                        f"绿灯分配变异系数 {cv:.2f} 较大，可能存在相位饥饿"
                    )

        score = self._compute_score(violations, warnings)
        return PriorCheckResult(
            passed=len(violations) == 0,
            violations=violations,
            warnings=warnings,
            score=score,
        )

    def check_phase_command(self, cmd_dict: dict) -> PriorCheckResult:
        """检查一个 PhaseCommand 的输出是否合理。

        Parameters
        ----------
        cmd_dict : dict
            PhaseCommand 的字典表示，应包含:
              - action: str
              - next_phase_id: int
              - duration: float
              - reason_code: str

        Returns
        -------
        PriorCheckResult
        """
        violations: List[str] = []
        warnings: List[str] = []

        ma = self.profile.micro_adjustment_prior
        pg = self.profile.phase_green_prior

        action = cmd_dict.get("action", "")
        duration = cmd_dict.get("duration", 0)
        next_phase_id = cmd_dict.get("next_phase_id")

        # ---- 检查 action 合法性 ----
        if action not in ("hold", "switch", "extend", "shorten"):
            violations.append(f"无效 action: {action}")

        # ---- 检查 duration ----
        if not _is_valid_number(duration):
            violations.append(f"duration={duration} 不是有效数值")
        elif duration < 0:
            violations.append(f"duration={duration} < 0")
        elif duration > pg.max_green_recommended * 1.5:
            violations.append(
                f"duration={duration:.1f}s 远超先验上限"
                f" {pg.max_green_recommended:.0f}s"
            )
        elif duration > pg.max_green_recommended:
            warnings.append(
                f"duration={duration:.1f}s 超过先验上限"
                f" {pg.max_green_recommended:.0f}s"
            )

        # ---- 检查 extend/shorten 幅度 ----
        if action == "extend" and _is_valid_number(duration):
            if duration > ma.max_extend_recommended * 2:
                violations.append(
                    f"extend duration={duration:.1f}s 超过先验"
                    f" {ma.max_extend_recommended * 2:.0f}s"
                )
            elif duration > ma.max_extend_recommended:
                warnings.append(
                    f"extend duration={duration:.1f}s 超过推荐"
                    f" {ma.max_extend_recommended:.0f}s"
                )

        if action == "shorten" and _is_valid_number(duration):
            if duration < pg.min_green_recommended * 0.3:
                violations.append(
                    f"shorten duration={duration:.1f}s 可能导致绿灯过短"
                )

        # ---- 检查 next_phase_id ----
        if next_phase_id is None:
            warnings.append("next_phase_id 为 None")

        # ---- 检查 reason_code ----
        if not cmd_dict.get("reason_code"):
            warnings.append("缺少 reason_code，建议提供决策原因")

        score = self._compute_score(violations, warnings)
        return PriorCheckResult(
            passed=len(violations) == 0,
            violations=violations,
            warnings=warnings,
            score=score,
        )

    # ==================================================================
    # 评分
    # ==================================================================

    def _compute_score(
        self, violations: List[str], warnings: List[str]
    ) -> float:
        """计算先验一致性分数。

        评分规则:
          - 基础分 1.0
          - 每个 violation 扣 0.3
          - 每个 warning 扣 0.1
          - 最低 0.0
        """
        score = 1.0
        score -= len(violations) * 0.3
        score -= len(warnings) * 0.1
        return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _extract_numeric_literals(code: str) -> List[float]:
    """从 Python 代码中提取数值字面量。"""
    # 匹配整数和浮点数（排除变量名中的数字）
    pattern = r"(?<![a-zA-Z_])(\d+\.?\d*|\.\d+)(?![a-zA-Z_])"
    matches = re.findall(pattern, code)
    numbers = []
    for m in matches:
        try:
            val = float(m)
            # 过滤明显不是交通参数的数值（如索引、版本号等）
            if val > 1.0:  # 忽略 0, 1 等常见索引
                numbers.append(val)
        except ValueError:
            continue
    return numbers


def _has_risky_division(code: str) -> bool:
    """检查代码中是否存在未保护的除法运算。"""
    # 查找除法运算，检查是否有 max(..., 0.01) 或类似的下界保护
    div_pattern = r"/\s*(?![//*])"  # 排除 // 和 /* 和 /
    divisions = list(re.finditer(div_pattern, code))

    for div_match in divisions:
        # 检查除号附近是否有保护
        start = max(0, div_match.start() - 50)
        end = min(len(code), div_match.end() + 50)
        context = code[start:end]

        # 如果除号在注释中，跳过
        line_start = code.rfind("\n", start, div_match.start())
        if line_start == -1:
            line_start = start
        line = code[line_start:div_match.start()]
        if "#" in line:
            continue

        # 检查除数是否是字面量正数（安全）
        after_div = code[div_match.end():div_match.end() + 20].strip()
        if re.match(r"^\d+\.?\d*$", after_div.split()[0] if after_div.split() else ""):
            continue

        # 检查是否有 max 保护
        if "max(" in context or "abs(" in context:
            continue

        # 可能是风险除法
        return True

    return False


def _is_valid_number(value) -> bool:
    """检查数值是否有效（非 NaN、非 inf、非负）。"""
    try:
        if isinstance(value, bool):
            return False
        if not isinstance(value, (int, float)):
            return False
        if math.isnan(value):
            return False
        if math.isinf(value):
            return False
        return True
    except (TypeError, ValueError):
        return False
