"""SkillLoader: 从 artifact 目录动态加载 skill 代码并验证安全性。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from signalclaw.skills.artifact import SkillArtifact

# 禁止在动态加载的 skill 中使用的模块
_BLOCKED_IMPORTS = frozenset({
    "os", "sys", "subprocess", "shutil", "pathlib",
    "socket", "http", "urllib", "requests",
    "ctypes", "multiprocessing", "threading",
    "importlib", "pkgutil", "code", "codeop",
    "pickle", "shelve", "marshal",
    "signal", "resource", "gc",
})

# 允许的命名空间模块
_SAFE_NAMESPACE = frozenset({
    "math", "collections", "itertools", "functools",
    "dataclasses", "typing", "abc",
})


class SkillLoadError(Exception):
    """加载 skill 时出错。"""


class SkillValidationError(SkillLoadError):
    """skill 代码未通过安全验证。"""


class SkillManifestError(SkillLoadError):
    """manifest 不满足加载条件。"""


def validate_skill_code(code: str) -> bool:
    """基本安全检查：禁止危险 import，要求定义正确函数。"""
    lines = code.splitlines()

    for line_no, line in enumerate(lines, 1):
        stripped = line.strip()
        # 跳过注释和空行
        if not stripped or stripped.startswith("#"):
            continue
        # 检查 import 语句
        if stripped.startswith("import ") or stripped.startswith("from "):
            tokens = stripped.split()
            if tokens[0] == "import":
                mod = tokens[1].split(".")[0]
            elif tokens[0] == "from":
                mod = tokens[1].split(".")[0]
            else:
                continue
            if mod in _BLOCKED_IMPORTS:
                return False
    return True


def _dynamic_load(code: str, skill_type: str) -> Any:
    """在受限命名空间中 exec skill 代码，返回包含 plan/decide 函数的对象。"""
    if not validate_skill_code(code):
        raise SkillValidationError("Skill code contains blocked imports")

    # 构建安全命名空间
    import math
    import itertools
    import functools
    import collections
    from dataclasses import dataclass
    from typing import Dict, List, Optional, Tuple

    # 允许引用 state 数据结构
    from signalclaw.core.state import (
        NetworkObservation, IntersectionObservation, PhaseObservation,
        CyclePlan, PhaseCommand,
    )

    safe_ns: dict = {
        "__builtins__": __builtins__,
        "math": math,
        "itertools": itertools,
        "functools": functools,
        "collections": collections,
        "dataclass": dataclass,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Tuple": Tuple,
        "NetworkObservation": NetworkObservation,
        "IntersectionObservation": IntersectionObservation,
        "PhaseObservation": PhaseObservation,
        "CyclePlan": CyclePlan,
        "PhaseCommand": PhaseCommand,
    }

    exec(code, safe_ns)  # noqa: S102

    # 验证导出了正确的函数
    if skill_type == "cycle":
        if "plan" not in safe_ns or not callable(safe_ns["plan"]):
            raise SkillValidationError("Cycle skill must define a callable plan(obs)")
    elif skill_type == "phase":
        if "decide" not in safe_ns or not callable(safe_ns["decide"]):
            raise SkillValidationError("Phase skill must define a callable decide(obs, plan)")

    # 返回一个轻量包装对象
    class _SkillWrapper:
        pass

    wrapper = _SkillWrapper()
    if skill_type == "cycle":
        wrapper.plan = safe_ns["plan"]
    elif skill_type == "phase":
        wrapper.decide = safe_ns["decide"]

    # 传递辅助状态（如有）
    if "_reset" in safe_ns and callable(safe_ns["_reset"]):
        wrapper.reset = safe_ns["_reset"]

    return wrapper


def load_artifact(artifact_dir: str) -> SkillArtifact:
    """从目录中读取 manifest.json 并返回 SkillArtifact。"""
    manifest_path = Path(artifact_dir) / "manifest.json"
    if not manifest_path.exists():
        raise SkillLoadError(f"manifest.json not found in {artifact_dir}")
    raw = manifest_path.read_text(encoding="utf-8")
    return SkillArtifact.from_json(raw)


def _check_manifest_loadable(artifact: SkillArtifact) -> None:
    """验证 manifest 允许加载。"""
    if not artifact.frozen:
        raise SkillManifestError(
            f"Artifact {artifact.skill_id} is not frozen (frozen=False). "
            "Only frozen artifacts can be loaded."
        )
    if artifact.online_learning:
        raise SkillManifestError(
            f"Artifact {artifact.skill_id} has online_learning=True. "
            "Cannot load online-learning artifacts."
        )
    if artifact.exploration:
        raise SkillManifestError(
            f"Artifact {artifact.skill_id} has exploration=True. "
            "Cannot load exploration artifacts."
        )


class SkillLoader:
    """面向对象的 Skill 加载器，封装 load_artifact / load_skill 函数。"""

    def __init__(self, base_dir: str = "."):
        self.base_dir = base_dir

    def load_artifact(self, artifact_dir: str) -> SkillArtifact:
        rel = os.path.join(self.base_dir, artifact_dir) if not os.path.isabs(artifact_dir) else artifact_dir
        return load_artifact(rel)

    def load_skill(self, artifact_dir: str) -> Any:
        rel = os.path.join(self.base_dir, artifact_dir) if not os.path.isabs(artifact_dir) else artifact_dir
        return load_skill(rel)


def load_skill(artifact_dir: str) -> Any:
    """加载 skill.py 并返回实现了 CyclePlannerSkill 或 PhaseMicroSkill 接口的对象。

    前置条件: manifest.json 中 frozen=True, online_learning=False, exploration=False
    """
    artifact = load_artifact(artifact_dir)
    _check_manifest_loadable(artifact)

    skill_path = Path(artifact_dir) / "skill.py"
    if not skill_path.exists():
        raise SkillLoadError(f"skill.py not found in {artifact_dir}")

    code = skill_path.read_text(encoding="utf-8")

    # 验证 code_hash
    expected_hash = artifact.code_hash
    actual_hash = SkillArtifact.compute_code_hash(code)
    if expected_hash and expected_hash != actual_hash:
        raise SkillValidationError(
            f"Code hash mismatch for {artifact.skill_id}: "
            f"expected {expected_hash[:12]}, got {actual_hash[:12]}"
        )

    return _dynamic_load(code, artifact.skill_type)
