"""SkillCohort: 一组已冻结 skill 的集合，用于网络级部署。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from signalclaw.skills.loader import load_skill, load_artifact
from signalclaw.skills.artifact import SkillArtifact


@dataclass
class SkillCohort:
    """一组跨交叉口的 skill 集合，每个交叉口包含一个 cycle + 一个 phase skill。"""

    cohort_id: str
    skills: Dict[str, Dict[str, str]]  # crossing_id -> {"cycle": path, "phase": path}
    frozen: bool = False
    glm_used_online: bool = False
    created_by: str = "manual"

    # 缓存：避免重复加载
    _cache: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> Dict:
        return {
            "cohort_id": self.cohort_id,
            "skills": self.skills,
            "frozen": self.frozen,
            "glm_used_online": self.glm_used_online,
            "created_by": self.created_by,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: Dict) -> "SkillCohort":
        return cls(
            cohort_id=d["cohort_id"],
            skills=d["skills"],
            frozen=d.get("frozen", False),
            glm_used_online=d.get("glm_used_online", False),
            created_by=d.get("created_by", "manual"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "SkillCohort":
        return cls.from_dict(json.loads(json_str))

    def save(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str) -> "SkillCohort":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Cohort file not found: {path}")
        raw = p.read_text(encoding="utf-8")
        return cls.from_json(raw)

    def _get_skill(self, crossing_id: str, skill_type: str) -> Any:
        """加载并缓存 skill 对象。"""
        cache_key = f"{crossing_id}:{skill_type}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if crossing_id not in self.skills:
            raise KeyError(f"Crossing {crossing_id} not in cohort {self.cohort_id}")
        entry = self.skills[crossing_id]
        if skill_type not in entry:
            raise KeyError(f"Skill type {skill_type} not found for crossing {crossing_id}")

        artifact_dir = entry[skill_type]
        obj = load_skill(artifact_dir)
        self._cache[cache_key] = obj
        return obj

    def get_cycle_skill(self, crossing_id: str) -> Any:
        return self._get_skill(crossing_id, "cycle")

    def get_phase_skill(self, crossing_id: str) -> Any:
        return self._get_skill(crossing_id, "phase")

    def get_artifacts(self) -> List[SkillArtifact]:
        """加载 cohort 中所有 artifact 的元数据。"""
        result = []
        for crossing_id, types in self.skills.items():
            for skill_type, path in types.items():
                result.append(load_artifact(path))
        return result
