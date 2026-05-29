"""SkillArchive - 候选 Skill 档案库。

管理所有进化过程中产生的候选 Skill，包括存储、检索、历史查询。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from signalclaw.evolution.ast_sandbox import ASTCheckResult
from signalclaw.evolution.evaluator_replay import ReplayReport


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ArchiveEntry:
    """一个候选 Skill 的完整进化记录。"""
    candidate_id: str
    crossing_id: str
    skill_type: str  # "cycle" | "phase"
    parent_ids: List[str] = field(default_factory=list)
    code: str = ""
    code_hash: str = ""
    prompt: str = ""
    prompt_hash: str = ""
    glm_model: str = ""
    generation: int = 0
    static_check: Optional[dict] = None  # ASTCheckResult 序列化
    prior_check: Optional[dict] = None  # PriorCheckResult 序列化
    replay_report: Optional[dict] = None  # ReplayReport 序列化
    sumo_report: Optional[dict] = None  # SUMOEvalReport 序列化
    selected: bool = False
    rejection_reason: str = ""
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.code_hash and self.code:
            self.code_hash = hashlib.sha256(self.code.encode("utf-8")).hexdigest()
        if not self.prompt_hash and self.prompt:
            self.prompt_hash = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        d = {
            "candidate_id": self.candidate_id,
            "crossing_id": self.crossing_id,
            "skill_type": self.skill_type,
            "parent_ids": self.parent_ids,
            "code": self.code,
            "code_hash": self.code_hash,
            "prompt": self.prompt,
            "prompt_hash": self.prompt_hash,
            "glm_model": self.glm_model,
            "generation": self.generation,
            "static_check": self.static_check,
            "prior_check": self.prior_check,
            "replay_report": self.replay_report,
            "sumo_report": self.sumo_report,
            "selected": self.selected,
            "rejection_reason": self.rejection_reason,
            "created_at": self.created_at,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ArchiveEntry":
        return cls(
            candidate_id=d["candidate_id"],
            crossing_id=d["crossing_id"],
            skill_type=d["skill_type"],
            parent_ids=d.get("parent_ids", []),
            code=d.get("code", ""),
            code_hash=d.get("code_hash", ""),
            prompt=d.get("prompt", ""),
            prompt_hash=d.get("prompt_hash", ""),
            glm_model=d.get("glm_model", ""),
            generation=d.get("generation", 0),
            static_check=d.get("static_check"),
            prior_check=d.get("prior_check"),
            replay_report=d.get("replay_report"),
            sumo_report=d.get("sumo_report"),
            selected=d.get("selected", False),
            rejection_reason=d.get("rejection_reason", ""),
            created_at=d.get("created_at", ""),
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "ArchiveEntry":
        return cls.from_dict(json.loads(json_str))

    def set_static_check(self, result: ASTCheckResult) -> None:
        """记录 AST 检查结果。"""
        self.static_check = {
            "passed": result.passed,
            "violations": result.violations,
            "warnings": result.warnings,
            "has_correct_interface": result.has_correct_interface,
            "complexity_score": result.complexity_score,
        }

    def set_prior_check(self, result: "PriorCheckResult") -> None:
        """记录先验一致性检查结果。"""
        self.prior_check = {
            "passed": result.passed,
            "violations": result.violations,
            "warnings": result.warnings,
            "score": result.score,
        }

    def set_replay_report(self, report: ReplayReport) -> None:
        """记录 Replay 评估结果。"""
        self.replay_report = {
            "candidate_id": report.candidate_id,
            "crossing_id": report.crossing_id,
            "skill_type": report.skill_type,
            "passed": report.passed,
            "violations": report.violations,
            "score": report.score,
            "failure_cases": report.failure_cases,
            "test_cases_run": report.test_cases_run,
        }

    def set_sumo_report(self, report: "SUMOEvalReport") -> None:
        """记录 SUMO 离线评估结果。"""
        from signalclaw.evolution.evaluator_sumo import SUMOEvalReport
        self.sumo_report = {
            "candidate_id": report.candidate_id,
            "crossing_id": report.crossing_id,
            "skill_type": report.skill_type,
            "passed": report.passed,
            "score": report.score,
            "metrics": report.metrics,
            "violations": report.violations,
            "failure_cases": report.failure_cases,
            "sim_duration": report.sim_duration,
            "seed": report.seed,
            "n_seeds": report.n_seeds,
        }


# ---------------------------------------------------------------------------
# SkillArchive
# ---------------------------------------------------------------------------

class SkillArchive:
    """候选 Skill 档案库，持久化存储所有进化历史。"""

    def __init__(self, archive_dir: str):
        self.archive_dir = archive_dir
        os.makedirs(archive_dir, exist_ok=True)
        self._entries: Dict[str, ArchiveEntry] = {}
        self._load_existing()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, entry: ArchiveEntry) -> None:
        """添加候选到档案。"""
        self._entries[entry.candidate_id] = entry
        self._save_entry(entry)

    def get(self, candidate_id: str) -> Optional[ArchiveEntry]:
        """根据 ID 获取候选。"""
        return self._entries.get(candidate_id)

    def get_best(
        self, crossing_id: str, skill_type: str
    ) -> Optional[ArchiveEntry]:
        """获取某个路口某种类型的最佳候选（selected=True 且 score 最高的）。"""
        candidates = [
            e for e in self._entries.values()
            if e.crossing_id == crossing_id
            and e.skill_type == skill_type
            and e.selected
            and e.replay_report is not None
            and e.replay_report.get("passed", False)
        ]
        if not candidates:
            return None
        # 按 replay score 排序
        candidates.sort(
            key=lambda e: e.replay_report.get("score", 0.0) if e.replay_report else 0.0,
            reverse=True,
        )
        return candidates[0]

    def get_latest(
        self, crossing_id: str, skill_type: str
    ) -> Optional[ArchiveEntry]:
        """获取某个路口某种类型的最新候选。"""
        candidates = [
            e for e in self._entries.values()
            if e.crossing_id == crossing_id and e.skill_type == skill_type
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda e: e.generation, reverse=True)
        return candidates[0]

    def get_history(
        self, crossing_id: str, skill_type: str
    ) -> List[ArchiveEntry]:
        """获取进化历史（按 generation 排序）。"""
        entries = [
            e for e in self._entries.values()
            if e.crossing_id == crossing_id and e.skill_type == skill_type
        ]
        entries.sort(key=lambda e: e.generation)
        return entries

    def get_all_for_crossing(self, crossing_id: str) -> List[ArchiveEntry]:
        """获取某个路口的所有候选。"""
        return [
            e for e in self._entries.values()
            if e.crossing_id == crossing_id
        ]

    def get_summary(self, crossing_id: str, skill_type: str) -> str:
        """生成某个路口某种类型的进化摘要（供 GLM prompt 使用）。"""
        history = self.get_history(crossing_id, skill_type)
        if not history:
            return ""

        lines = [f"共 {len(history)} 代进化记录："]
        for entry in history:
            status = "SELECTED" if entry.selected else "REJECTED"
            score = 0.0
            if entry.replay_report:
                score = entry.replay_report.get("score", 0.0)
            violations = []
            if entry.replay_report:
                violations = entry.replay_report.get("violations", [])
            lines.append(
                f"  gen={entry.generation} [{status}] score={score:.4f}"
                f" violations={len(violations)}"
            )
            if entry.rejection_reason:
                lines.append(f"    拒绝原因: {entry.rejection_reason}")
            if violations:
                lines.append(f"    违规: {'; '.join(violations[:3])}")

        return "\n".join(lines)

    def save(self) -> None:
        """保存所有 entries 到磁盘。"""
        for entry in self._entries.values():
            self._save_entry(entry)

    def count(self) -> int:
        """档案中的总数。"""
        return len(self._entries)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _save_entry(self, entry: ArchiveEntry) -> None:
        """将单个 entry 保存到磁盘。"""
        entry_dir = os.path.join(
            self.archive_dir,
            entry.crossing_id,
            entry.skill_type,
            entry.candidate_id,
        )
        os.makedirs(entry_dir, exist_ok=True)

        # 保存代码
        if entry.code:
            code_path = os.path.join(entry_dir, "skill.py")
            with open(code_path, "w", encoding="utf-8") as f:
                f.write(entry.code)

        # 保存元数据
        meta_path = os.path.join(entry_dir, "entry.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(entry.to_json())

    def _load_existing(self) -> None:
        """从磁盘加载已有的 entries。"""
        archive_path = Path(self.archive_dir)
        if not archive_path.exists():
            return

        for crossing_dir in archive_path.iterdir():
            if not crossing_dir.is_dir():
                continue
            for type_dir in crossing_dir.iterdir():
                if not type_dir.is_dir():
                    continue
                for entry_dir in type_dir.iterdir():
                    if not entry_dir.is_dir():
                        continue
                    meta_path = entry_dir / "entry.json"
                    if meta_path.exists():
                        try:
                            raw = meta_path.read_text(encoding="utf-8")
                            entry = ArchiveEntry.from_json(raw)
                            self._entries[entry.candidate_id] = entry
                        except (json.JSONDecodeError, KeyError, TypeError):
                            continue
