"""PromptBuilder - 为每个路口构造 GLM 进化 prompt。

所有 prompt 模板集中在此模块管理，确保 GLM 生成的代码满足安全约束。
支持注入 SQL 参考画像信息作为先验提示，帮助 GLM 生成更贴近真实交通的代码。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from signalclaw.reference.profile_schema import SQLReferenceProfile


class PromptBuilder:
    """为 Cycle/Phase Skill 进化构造 (system_prompt, user_prompt) 对。

    Parameters
    ----------
    sql_profile : SQLReferenceProfile, optional
        SQL 参考画像，包含真实交通系统的统计先验。
        如果提供，会在 prompt 中注入先验信息供 GLM 参考。
    """

    def __init__(self, sql_profile: Optional["SQLReferenceProfile"] = None):
        self.sql_profile = sql_profile

    # ======================================================================
    # System prompt 模板
    # ======================================================================

    SYSTEM_PROMPT_CYCLE = (
        "你是一个交通信号控制算法专家。你的任务是改进一个交叉口的周期规划算法（CyclePlannerSkill）。\n"
        "\n"
        "## 你的任务\n"
        "基于父代算法代码和路口特征，生成一个改进版的 plan() 函数。\n"
        "\n"
        "## 输入输出接口\n"
        "plan(obs) 函数:\n"
        "  参数:\n"
        "    obs: NetworkObservation 对象，包含:\n"
        "      - obs.ego: IntersectionObservation（当前路口状态）\n"
        "        - obs.ego.crossing_id: str（路口 ID）\n"
        "        - obs.ego.phases: Dict[int, PhaseObservation]（各相位状态）\n"
        "          - phase_obs.queue: float（排队车辆数）\n"
        "          - phase_obs.waiting_time: float（平均等待时间，秒）\n"
        "          - phase_obs.predicted_arrival: float（预测到达车辆数）\n"
        "          - phase_obs.elapsed_green: float（已运行绿灯时间）\n"
        "          - phase_obs.min_green: float（最小绿灯时间）\n"
        "          - phase_obs.max_green: float（最大绿灯时间）\n"
        "          - phase_obs.saturation_flow: float（饱和流率）\n"
        "        - obs.ego.downstream_queue: Dict[str, float]（下游排队）\n"
        "        - obs.ego.upstream_queue: Dict[str, float]（上游排队）\n"
        "        - obs.ego.downstream_spillback_risk: float（下游溢出风险）\n"
        "        - obs.ego.upstream_release_pressure: float（上游释放压力）\n"
        "      - obs.neighbors: Dict[str, IntersectionObservation]（邻居路口）\n"
        "      - obs.timestamp: float（仿真时间）\n"
        "  返回:\n"
        "    CyclePlan(cycle_length, green_times, phase_order, offset_target)\n"
        "\n"
        "## 你必须遵循以下规则\n"
        "1. 你只能修改 plan() 函数体及其辅助函数\n"
        "2. 你不能 import 任何模块（math 除外，使用 from math import ... 也不行）\n"
        "3. 你不能读取文件或调用网络\n"
        "4. 你不能使用随机数（random, numpy.random）\n"
        "5. 你必须返回 CyclePlan 对象\n"
        "6. 你必须满足 min_green 和 max_green 约束\n"
        "7. 你必须满足 min_cycle 和 max_cycle 约束\n"
        "8. 你的代码必须是确定性的（每次相同输入产生相同输出）\n"
        "9. 你只能使用以下内置函数: min, max, sum, abs, sorted, len, range, enumerate, float, int, dict, list, tuple, set, bool, str, isinstance, round, zip, map, filter, any, all, reversed, print\n"
        "10. 你可以使用 math 模块中的函数（但必须通过 import math 引入）\n"
        "11. 不能使用类（class），不能使用全局可变状态\n"
        "12. 所有变量必须初始化后才能使用\n"
        "13. 不能使用 try/except\n"
        "\n"
        "## 输出格式\n"
        "请严格按以下 JSON 格式输出（不要输出其他内容）：\n"
        '{"rationale": "简要说明你的改进思路（1-3 句话）", "expected_effect": "预期效果", "risk": "可能的副作用或风险", "code": "完整的 plan() 函数代码（包含所有辅助函数）"}\n'
    )

    SYSTEM_PROMPT_PHASE = (
        "你是一个交通信号控制算法专家。你的任务是改进一个交叉口的相位微调算法（PhaseMicroSkill）。\n"
        "\n"
        "## 你的任务\n"
        "基于父代算法代码、路口特征和配对的 CyclePlan 算法，生成一个改进版的 decide() 函数。\n"
        "\n"
        "## 输入输出接口\n"
        "decide(obs, plan) 函数:\n"
        "  参数:\n"
        "    obs: NetworkObservation（同 Cycle Skill）\n"
        "    plan: CyclePlan\n"
        "      - plan.cycle_length: float\n"
        "      - plan.green_times: Dict[int, float]（phase_id -> green seconds）\n"
        "      - plan.phase_order: List[int]\n"
        "      - plan.offset_target: Optional[float]\n"
        "  返回:\n"
        "    PhaseCommand(action, next_phase_id, duration, reason_code)\n"
        "    action: \"hold\" | \"switch\" | \"extend\" | \"shorten\"\n"
        "\n"
        "## 你必须遵循以下规则\n"
        "1. 你只能修改 decide() 函数体及其辅助函数\n"
        "2. 你不能 import 任何模块（math 除外，使用 from math import ... 也不行）\n"
        "3. 你不能读取文件或调用网络\n"
        "4. 你不能使用随机数（random, numpy.random）\n"
        "5. 你必须返回 PhaseCommand 对象\n"
        "6. 你不能让 duration 为负数或超过 max_green\n"
        "7. next_phase_id 必须是 plan.phase_order 中存在的值\n"
        "8. 你的代码必须是确定性的\n"
        "9. 你只能使用以下内置函数: min, max, sum, abs, sorted, len, range, enumerate, float, int, dict, list, tuple, set, bool, str, isinstance, round, zip, map, filter, any, all, reversed, print\n"
        "10. 你可以使用 math 模块中的函数（但必须通过 import math 引入）\n"
        "11. 不能使用类（class），不能使用全局可变状态\n"
        "12. 所有变量必须初始化后才能使用\n"
        "13. 不能使用 try/except\n"
        "\n"
        "## 输出格式\n"
        "请严格按以下 JSON 格式输出（不要输出其他内容）：\n"
        '{"rationale": "简要说明你的改进思路（1-3 句话）", "expected_effect": "预期效果", "risk": "可能的副作用或风险", "code": "完整的 decide() 函数代码（包含所有辅助函数）"}\n'
    )

    # ======================================================================
    # Public API
    # ======================================================================

    def build_cycle_prompt(
        self,
        crossing_profile: str,
        parent_code: str,
        failure_cases: list,
        constraints: str,
        archive_summary: str,
        sql_profile: Optional["SQLReferenceProfile"] = None,
    ) -> Tuple[str, str]:
        """构建周期 Skill 进化 prompt。

        Parameters
        ----------
        crossing_profile : str
            路口拓扑描述
        parent_code : str
            父代算法代码
        failure_cases : list
            历史失败案例
        constraints : str
            约束条件
        archive_summary : str
            历史进化摘要
        sql_profile : SQLReferenceProfile, optional
            SQL 参考画像（覆盖实例级别的 sql_profile）

        Returns
        -------
        (system_prompt, user_prompt)
        """
        user_prompt = (
            f"# 路口特征\n"
            f"{crossing_profile}\n"
            f"\n"
            f"# 约束条件\n"
            f"{constraints}\n"
            f"\n"
            f"# 父代算法代码\n"
            f"```python\n"
            f"{parent_code}\n"
            f"```\n"
            f"\n"
            f"# 历史失败案例\n"
            f"{self._format_failure_cases(failure_cases)}\n"
            f"\n"
            f"# 历史进化摘要\n"
            f"{self._format_archive_summary(archive_summary)}\n"
        )

        # 注入 SQL 参考先验
        profile = sql_profile or self.sql_profile
        if profile is not None:
            prior_text = profile.to_prompt_text()
            user_prompt += (
                f"\n"
                f"---\n"
                f"\n"
                f"# 真实交通系统统计先验（参考性质，非强制约束）\n"
                f"以下是来自真实交通系统的统计先验，请参考这些规律但不要照搬：\n"
                f"{prior_text}\n"
            )

        user_prompt += (
            f"\n"
            f"---\n"
            f"\n"
            f"请基于以上信息，改进父代算法。重点关注：\n"
            f"1. 如果有失败案例，优先修复导致失败的问题\n"
            f"2. 在保证安全约束的前提下优化绿灯分配效率\n"
            f"3. 考虑下游溢出风险和邻居路口协调\n"
            f"4. 避免相位饥饿（每个相位都要有合理的绿灯时间）\n"
        )

        # 如果有先验，增加额外的指导提示
        if profile is not None:
            user_prompt += (
                f"5. 参考真实系统的周期和绿灯统计范围，但根据当前路口特征灵活调整\n"
            )

        user_prompt += f"\n请严格按 JSON 格式输出。\n"

        return self.SYSTEM_PROMPT_CYCLE, user_prompt

    def build_phase_prompt(
        self,
        crossing_profile: str,
        parent_code: str,
        paired_cycle_code: str,
        failure_cases: list,
        constraints: str,
        archive_summary: str,
        sql_profile: Optional["SQLReferenceProfile"] = None,
    ) -> Tuple[str, str]:
        """构建相位 Skill 进化 prompt。

        Parameters
        ----------
        crossing_profile : str
            路口拓扑描述
        parent_code : str
            父代算法代码
        paired_cycle_code : str
            配对的 CyclePlan 算法代码
        failure_cases : list
            历史失败案例
        constraints : str
            约束条件
        archive_summary : str
            历史进化摘要
        sql_profile : SQLReferenceProfile, optional
            SQL 参考画像（覆盖实例级别的 sql_profile）

        Returns
        -------
        (system_prompt, user_prompt)
        """
        user_prompt = (
            f"# 路口特征\n"
            f"{crossing_profile}\n"
            f"\n"
            f"# 约束条件\n"
            f"{constraints}\n"
            f"\n"
            f"# 配对的 CyclePlan 算法代码（只读参考）\n"
            f"```python\n"
            f"{paired_cycle_code}\n"
            f"```\n"
            f"\n"
            f"# 父代算法代码\n"
            f"```python\n"
            f"{parent_code}\n"
            f"```\n"
            f"\n"
            f"# 历史失败案例\n"
            f"{self._format_failure_cases(failure_cases)}\n"
            f"\n"
            f"# 历史进化摘要\n"
            f"{self._format_archive_summary(archive_summary)}\n"
        )

        # 注入 SQL 参考先验
        profile = sql_profile or self.sql_profile
        if profile is not None:
            prior_text = profile.to_prompt_text()
            user_prompt += (
                f"\n"
                f"---\n"
                f"\n"
                f"# 真实交通系统统计先验（参考性质，非强制约束）\n"
                f"以下是来自真实交通系统的统计先验，请参考这些规律但不要照搬：\n"
                f"{prior_text}\n"
            )

        user_prompt += (
            f"\n"
            f"---\n"
            f"\n"
            f"请基于以上信息，改进父代相位微调算法。重点关注：\n"
            f"1. 与配对的 CyclePlan 算法协调配合\n"
            f"2. 如果有失败案例，优先修复\n"
            f"3. 合理使用 extend/shorten 进行动态微调\n"
            f"4. 避免频繁切换相位（增加黄灯损失时间）\n"
            f"5. 考虑下游排队状况，避免溢出\n"
        )

        # 如果有先验，增加额外的指导提示
        if profile is not None:
            user_prompt += (
                f"6. 参考真实系统的微调幅度和条件，但根据当前路口特征灵活调整\n"
            )

        user_prompt += f"\n请严格按 JSON 格式输出。\n"

        return self.SYSTEM_PROMPT_PHASE, user_prompt

    # ======================================================================
    # Internal helpers
    # ======================================================================

    def _format_failure_cases(self, failure_cases: list) -> str:
        """将失败案例格式化为可读文本。"""
        if not failure_cases:
            return "（无历史失败案例）"

        lines = []
        for i, fc in enumerate(failure_cases[:10], 1):  # 最多展示 10 个
            if isinstance(fc, dict):
                violation = fc.get("violation", "未知")
                detail = fc.get("detail", "")
                lines.append(f"  {i}. {violation}: {detail}")
            else:
                lines.append(f"  {i}. {fc}")

        return "\n".join(lines)

    def _format_archive_summary(self, archive_summary: str) -> str:
        """将进化历史摘要格式化。"""
        if not archive_summary:
            return "（首次进化，无历史记录）"
        return archive_summary

    def _format_constraints(self, constraints) -> str:
        """将约束条件格式化为可读文本（供外部调用）。"""
        if isinstance(constraints, str):
            return constraints
        if hasattr(constraints, "__dataclass_fields__"):
            # IntersectionConstraints
            parts = []
            for field_name in constraints.__dataclass_fields__:
                val = getattr(constraints, field_name)
                parts.append(f"  {field_name}: {val}")
            return "\n".join(parts)
        return str(constraints)
