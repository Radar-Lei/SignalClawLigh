"""Skill registry: 内存注册表 + artifact 目录注册。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from signalclaw.skills.artifact import SkillArtifact
from signalclaw.skills.loader import load_artifact

# 内存 class 注册表
SKILL_REGISTRY: Dict[str, Any] = {}

# artifact 目录注册表
_ARTIFACT_REGISTRY: Dict[str, str] = {}  # name -> artifact_dir


def register_skill(name: str):
    """装饰器：注册 skill class。"""
    def decorator(cls):
        SKILL_REGISTRY[name] = cls
        return cls
    return decorator


def get_skill(name: str):
    """获取已注册的 skill class。"""
    if name not in SKILL_REGISTRY:
        raise KeyError(f"Skill '{name}' not found. Available: {list(SKILL_REGISTRY.keys())}")
    return SKILL_REGISTRY[name]


def register_artifact(name: str, artifact_dir: str) -> None:
    """注册一个 artifact 目录。"""
    _ARTIFACT_REGISTRY[name] = artifact_dir


def get_artifact(name: str) -> SkillArtifact:
    """获取已注册 artifact 的元数据。"""
    if name not in _ARTIFACT_REGISTRY:
        raise KeyError(
            f"Artifact '{name}' not found. Available: {list(_ARTIFACT_REGISTRY.keys())}"
        )
    return load_artifact(_ARTIFACT_REGISTRY[name])


def list_artifacts() -> List[str]:
    """列出所有已注册的 artifact 名称。"""
    return list(_ARTIFACT_REGISTRY.keys())


def get_artifact_dir(name: str) -> str:
    """获取已注册 artifact 的目录路径。"""
    if name not in _ARTIFACT_REGISTRY:
        raise KeyError(
            f"Artifact '{name}' not found. Available: {list(_ARTIFACT_REGISTRY.keys())}"
        )
    return _ARTIFACT_REGISTRY[name]


def clear_artifacts() -> None:
    """清空 artifact 注册表（主要用于测试）。"""
    _ARTIFACT_REGISTRY.clear()
