"""GLMSkillMutator - 唯一负责调用 GLM 生成/修改 Skill 代码的模块。

只有本模块可以 import glm_client，其他 evolution 模块一律通过本模块间接调用。
"""

from __future__ import annotations

import json
import re
import sys
import os
from dataclasses import dataclass, field
from typing import Optional

# 确保项目根目录在 sys.path 中
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from glm_client import GLMClient


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CandidateSkill:
    """GLM 生成的候选 Skill。"""
    code: str
    rationale: str = ""
    expected_effect: str = ""
    risk: str = ""
    source: str = "glm"  # "glm" | "seed" | "manual"


# ---------------------------------------------------------------------------
# GLMSkillMutator
# ---------------------------------------------------------------------------

class GLMSkillMutator:
    """调用 GLM API 变异 Cycle/Phase Skill。"""

    def __init__(
        self,
        glm_client: Optional[GLMClient] = None,
        temperature: float = 0.5,
        max_tokens: int = 16384,
    ):
        self.client = glm_client or GLMClient()
        self.temperature = temperature
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mutate_cycle_skill(
        self,
        crossing_profile: str,
        parent_skill_code: str,
        failure_cases: list,
        constraints: str,
        archive_summary: str,
    ) -> CandidateSkill:
        """调用 GLM 变异周期 Skill。

        Parameters
        ----------
        crossing_profile : str
            路口拓扑描述（相位数、车道数等）
        parent_skill_code : str
            父代 Skill 代码
        failure_cases : list
            历史失败案例列表
        constraints : str
            约束条件的字符串描述
        archive_summary : str
            历史进化摘要

        Returns
        -------
        CandidateSkill
        """
        from signalclaw.evolution.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        system_prompt, user_prompt = builder.build_cycle_prompt(
            crossing_profile=crossing_profile,
            parent_code=parent_skill_code,
            failure_cases=failure_cases,
            constraints=constraints,
            archive_summary=archive_summary,
        )
        raw_response = self._call_glm(system_prompt, user_prompt)
        return self._parse_response(raw_response)

    def mutate_phase_skill(
        self,
        crossing_profile: str,
        parent_skill_code: str,
        paired_cycle_skill_code: str,
        failure_cases: list,
        constraints: str,
        archive_summary: str,
    ) -> CandidateSkill:
        """调用 GLM 变异相位 Skill。

        Parameters
        ----------
        crossing_profile : str
            路口拓扑描述
        parent_skill_code : str
            父代 Phase Skill 代码
        paired_cycle_skill_code : str
            配对的 Cycle Skill 代码（只读参考）
        failure_cases : list
            历史失败案例
        constraints : str
            约束条件描述
        archive_summary : str
            历史进化摘要

        Returns
        -------
        CandidateSkill
        """
        from signalclaw.evolution.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        system_prompt, user_prompt = builder.build_phase_prompt(
            crossing_profile=crossing_profile,
            parent_code=parent_skill_code,
            paired_cycle_code=paired_cycle_skill_code,
            failure_cases=failure_cases,
            constraints=constraints,
            archive_summary=archive_summary,
        )
        raw_response = self._call_glm(system_prompt, user_prompt)
        return self._parse_response(raw_response)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call_glm(self, system_prompt: str, user_prompt: str) -> str:
        """调用 GLM API 并返回原始响应文本。带重试逻辑。"""
        import time
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                result = self.client.chat(
                    user_prompt,
                    system_message=system_prompt,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
                if result and result.strip():
                    return result
                # 空响应，可能是 reasoning 消耗了所有 tokens
                print(f"[glm_mutator] 警告: GLM 返回空响应 (attempt {attempt + 1}/{max_retries + 1})")
            except Exception as e:
                print(f"[glm_mutator] GLM 调用异常 (attempt {attempt + 1}/{max_retries + 1}): {e}")
            if attempt < max_retries:
                time.sleep(2)
        return ""

    def _parse_response(self, response: str) -> CandidateSkill:
        """解析 GLM 响应，提取代码块与元信息。

        尝试以下顺序提取代码：
        1. 去除 markdown 包装后解析 JSON
        2. 直接搜索 ```json ... ``` 中的 JSON
        3. Markdown fenced code block 中的 Python 代码
        4. 整段文本（作为降级处理）
        """
        if not response or not response.strip():
            return CandidateSkill(code="", rationale="empty response", source="glm")

        # ---- 尝试 0: 去除 markdown ```json ``` 包装 ----
        cleaned = response.strip()
        if cleaned.startswith("```"):
            # 去除外层 markdown code block
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)

        # ---- 尝试 1: JSON 解析 ----
        # 使用更精确的 JSON 提取：从第一个 { 到最后一个 }
        json_start = cleaned.find("{")
        json_end = cleaned.rfind("}")
        if json_start >= 0 and json_end > json_start:
            json_str = cleaned[json_start:json_end + 1]
            try:
                data = json.loads(json_str)
                if "code" in data and isinstance(data["code"], str):
                    return CandidateSkill(
                        code=data["code"],
                        rationale=data.get("rationale", ""),
                        expected_effect=data.get("expected_effect", ""),
                        risk=data.get("risk", ""),
                        source="glm",
                    )
            except (json.JSONDecodeError, TypeError):
                pass

        # ---- 尝试 2: Markdown code block (python) ----
        code_block_pattern = re.compile(
            r"```(?:python)?\s*\n([\s\S]*?)```", re.MULTILINE
        )
        matches = code_block_pattern.findall(response)
        if matches:
            # 取最长的代码块作为主代码
            code = max(matches, key=len).strip()
            # 尝试从剩余文本提取 rationale
            remaining = code_block_pattern.sub("", response).strip()
            return CandidateSkill(
                code=code,
                rationale=remaining[:500] if remaining else "",
                source="glm",
            )

        # ---- 尝试 3: 搜索 "code": "..." 模式 ----
        code_match = re.search(r'"code"\s*:\s*"((?:[^"\\]|\\.)*)"', response)
        if code_match:
            raw_code = code_match.group(1)
            # 解码 JSON 字符串转义
            try:
                decoded_code = json.loads(f'"{raw_code}"')
                return CandidateSkill(
                    code=decoded_code,
                    rationale="",
                    source="glm",
                )
            except (json.JSONDecodeError, TypeError):
                pass

        # ---- 尝试 4: 整段降级 ----
        return CandidateSkill(
            code=response.strip(),
            rationale="fallback: raw response used as code",
            source="glm",
        )
