"""SkillArtifact: 每个 skill 版本的不可变元数据记录。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class SkillMetrics:
    replay_score: float = 0.0
    sumo_score: float = 0.0
    safety_violations: int = 0
    phase_starvation_count: int = 0
    mean_waiting: float = 0.0
    mean_queue: float = 0.0
    throughput: float = 0.0

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "SkillMetrics":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class SkillArtifact:
    skill_id: str
    crossing_id: str
    skill_type: str  # "cycle" | "phase"
    version: int
    parent_skill_ids: List[str] = field(default_factory=list)
    code_hash: str = ""
    prompt_hash: str = ""
    data_split_hash: str = ""
    sumo_scenario_hash: str = ""
    glm_model: str = ""
    created_at: str = ""
    frozen: bool = False
    online_learning: bool = False
    exploration: bool = False
    constraints_profile: str = "default"
    metrics: SkillMetrics = field(default_factory=SkillMetrics)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def make_skill_id(self) -> str:
        """Generate canonical skill id: tls_{crossing_id}_{skill_type}_v{version:04d}"""
        return f"tls_{self.crossing_id}_{self.skill_type}_v{self.version:04d}"

    @staticmethod
    def compute_code_hash(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["metrics"] = self.metrics.to_dict()
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: Dict) -> "SkillArtifact":
        metrics_d = d.pop("metrics", {})
        metrics = SkillMetrics.from_dict(metrics_d)
        return cls(metrics=metrics, **{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_json(cls, json_str: str) -> "SkillArtifact":
        return cls.from_dict(json.loads(json_str))

    def refresh_skill_id(self) -> None:
        """Re-generate skill_id from current fields."""
        self.skill_id = self.make_skill_id()
