"""SkillCohort: 一组已冻结 skill 的集合，用于网络级部署。"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from signalclaw.skills.loader import load_skill, load_artifact
from signalclaw.skills.artifact import SkillArtifact

logger = logging.getLogger(__name__)


def _find_project_root() -> Optional[Path]:
    """从 cwd 向上查找含有 .git 或 pyproject.toml 的目录作为项目根目录。"""
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
            return parent
    return None


def _resolve_path(raw_path: str) -> str:
    """解析 skill 路径，支持绝对路径和 repo-relative 相对路径。

    解析策略：
    1. 绝对路径且文件存在 → 直接使用
    2. 相对路径 → 先尝试相对于 cwd
    3. 不存在 → 尝试相对于项目根目录
    4. 都不存在 → 保持原路径（让后续 FileNotFoundError 报错）
    """
    p = Path(raw_path)
    # 绝对路径且存在，直接使用
    if p.is_absolute() and p.exists():
        return raw_path
    # 相对路径：先尝试 cwd
    if not p.is_absolute():
        cwd_resolved = Path.cwd() / p
        if cwd_resolved.exists():
            return str(cwd_resolved)
        # 再尝试项目根目录
        project_root = _find_project_root()
        if project_root is not None:
            root_resolved = project_root / p
            if root_resolved.exists():
                return str(root_resolved)
    # 无法解析，保持原路径
    return raw_path


@dataclass
class SkillCohort:
    """一组跨交叉口的 skill 集合，每个交叉口包含一个 cycle + 一个 phase skill。"""

    cohort_id: str
    skills: Dict[str, Dict[str, str]]  # crossing_id -> {"cycle": path, "phase": path}
    frozen: bool = False
    glm_used_online: bool = False
    exploration: bool = False
    created_by: str = "manual"
    source: str = "manual"  # "sealed_sumo_champion" | "archive_only" | "seed_fallback" | "manual"
    all_skills_accepted_for_deployment: bool = False

    # 缓存：避免重复加载
    _cache: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> Dict:
        return {
            "cohort_id": self.cohort_id,
            "skills": self.skills,
            "frozen": self.frozen,
            "glm_used_online": self.glm_used_online,
            "exploration": self.exploration,
            "created_by": self.created_by,
            "source": self.source,
            "all_skills_accepted_for_deployment": self.all_skills_accepted_for_deployment,
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
            exploration=d.get("exploration", False),
            created_by=d.get("created_by", "manual"),
            source=d.get("source", "manual"),
            all_skills_accepted_for_deployment=d.get("all_skills_accepted_for_deployment", False),
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

        artifact_dir = _resolve_path(entry[skill_type])
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
                resolved = _resolve_path(path)
                result.append(load_artifact(resolved))
        return result

    def filter_deployable(
        self,
        archive_dir: Optional[str] = None,
        seed_cohort: Optional["SkillCohort"] = None,
    ) -> "SkillCohort":
        """过滤出只包含可部署 skill 的 cohort。

        部署门槛（三重硬门槛）：
        1. accepted_for_deployment = True
        2. has_real_sumo_report = True
        3. paired_eval_passed = True

        不满足条件的 crossing 会退回到 seed_cohort 中的对应 skill。
        如果 seed_cohort 也无对应 skill，则从结果中移除该 crossing。

        Parameters
        ----------
        archive_dir : str, optional
            evolution archive 目录路径，用于查找 entry.json 检查部署状态。
        seed_cohort : SkillCohort, optional
            seed cohort，用于在 evolved skill 不满足部署门槛时退回。

        Returns
        -------
        SkillCohort
            只包含通过部署门槛 skill 的新 cohort。
        """
        deployable_skills: Dict[str, Dict[str, str]] = {}

        for crossing_id, types in self.skills.items():
            deployable_types: Dict[str, str] = {}

            for skill_type, skill_path in types.items():
                # 检查该 skill 的 manifest 是否通过部署门槛
                if self._check_skill_deployable(skill_path, archive_dir):
                    deployable_types[skill_type] = skill_path
                elif seed_cohort and crossing_id in seed_cohort.skills:
                    # 退回到 seed
                    seed_types = seed_cohort.skills[crossing_id]
                    if skill_type in seed_types:
                        seed_path = seed_types[skill_type]
                        logger.info(
                            "cohort %s: crossing=%s type=%s 不满足部署门槛，"
                            "退回 seed path=%s",
                            self.cohort_id, crossing_id, skill_type, seed_path,
                        )
                        deployable_types[skill_type] = seed_path
                    else:
                        logger.warning(
                            "cohort %s: crossing=%s type=%s 不满足部署门槛且 "
                            "seed 中无对应类型，跳过",
                            self.cohort_id, crossing_id, skill_type,
                        )
                else:
                    logger.warning(
                        "cohort %s: crossing=%s type=%s 不满足部署门槛且 "
                        "无 seed cohort，跳过",
                        self.cohort_id, crossing_id, skill_type,
                    )

            if deployable_types:
                deployable_skills[crossing_id] = deployable_types

        return SkillCohort(
            cohort_id=f"{self.cohort_id}_deployable",
            skills=deployable_skills,
            frozen=True,
            glm_used_online=self.glm_used_online,
            created_by=f"{self.created_by}+deployable_filter",
        )

    @staticmethod
    def _check_skill_deployable(skill_path: str, archive_dir: Optional[str] = None) -> bool:
        """检查单个 skill 是否通过部署门槛。

        优先查找 skill_path 目录下的 manifest.json / entry.json，
        其次在 archive_dir 中查找。

        三重硬门槛：accepted_for_deployment + has_real_sumo_report + paired_eval_passed
        """
        skill_p = Path(skill_path)

        # 策略 1：直接在 skill_path 下查找 manifest
        for manifest_name in ("manifest.json", "entry.json"):
            manifest_path = skill_p / manifest_name
            if manifest_path.exists():
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    return (
                        data.get("accepted_for_deployment", False) is True
                        and data.get("has_real_sumo_report", False) is True
                        and data.get("paired_eval_passed", False) is True
                    )
                except (json.JSONDecodeError, KeyError):
                    continue

        # 策略 2：在 archive_dir 下查找
        if archive_dir:
            # 从路径推断 crossing_id 和 skill_type
            # skill_path 的典型格式: .../evolved_skills/{crossing_id}/{skill_type}/v{version}
            parts = skill_p.parts
            for i, part in enumerate(parts):
                if part in ("cycle", "phase") and i > 0:
                    crossing_id = parts[i - 1]
                    skill_type = part
                    # 查找 archive 下的 entry
                    entry_dir = Path(archive_dir) / crossing_id / skill_type
                    if entry_dir.exists():
                        for sub in entry_dir.iterdir():
                            entry_path = sub / "entry.json"
                            if entry_path.exists():
                                try:
                                    data = json.loads(entry_path.read_text(encoding="utf-8"))
                                    # 匹配 code_hash
                                    if (
                                        data.get("accepted_for_deployment", False) is True
                                        and data.get("has_real_sumo_report", False) is True
                                        and data.get("paired_eval_passed", False) is True
                                    ):
                                        return True
                                except (json.JSONDecodeError, KeyError):
                                    continue
                    break

        # 默认：无法验证部署状态 → 不可部署
        return False


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def load_deployable_cohort(
    evolved_cohort_path: str,
    seed_cohort_path: str,
    archive_dir: Optional[str] = None,
) -> SkillCohort:
    """加载只包含通过部署门槛 skill 的 evolved cohort。

    如果 evolved cohort 中的 skill 未通过三重硬门槛
    (accepted_for_deployment + has_real_sumo_report + paired_eval_passed)，
    则退回到 seed cohort 中对应的 skill。

    Parameters
    ----------
    evolved_cohort_path : str
        evolved cohort JSON 文件路径。
    seed_cohort_path : str
        seed cohort JSON 文件路径（作为退回 baseline）。
    archive_dir : str, optional
        evolution archive 目录路径，用于查找 entry.json。

    Returns
    -------
    SkillCohort
        只包含可部署 skill 的 cohort。
    """
    evolved = SkillCohort.load(evolved_cohort_path)
    seed = SkillCohort.load(seed_cohort_path)
    return evolved.filter_deployable(archive_dir=archive_dir, seed_cohort=seed)


def archive_evolved_cohort(
    evolved_cohort_path: str,
    version: int = 1,
    dry_run: bool = False,
) -> Optional[str]:
    """将当前 evolved cohort 降级为版本化 archive 文件。

    将 evolved_cohort.json 重命名为 archive_evolved_candidates_v{version:03d}.json，
    使其不再被自动加载为活跃 cohort。

    Parameters
    ----------
    evolved_cohort_path : str
        当前 evolved cohort JSON 文件路径。
    version : int
        archive 版本号（默认 1）。
    dry_run : bool
        如果为 True，只返回目标路径但不执行重命名。

    Returns
    -------
    str or None
        archive 文件的目标路径；如果源文件不存在则返回 None。
    """
    src = Path(evolved_cohort_path)
    if not src.exists():
        logger.info("archive_evolved_cohort: %s 不存在，跳过", evolved_cohort_path)
        return None

    dst = src.parent / f"archive_evolved_candidates_v{version:03d}.json"
    if dst.exists():
        # 已存在同版本 archive，自动递增版本号
        v = version + 1
        while (src.parent / f"archive_evolved_candidates_v{v:03d}.json").exists():
            v += 1
        dst = src.parent / f"archive_evolved_candidates_v{v:03d}.json"

    if dry_run:
        logger.info(
            "archive_evolved_cohort [dry_run]: 将 %s → %s",
            src, dst,
        )
        return str(dst)

    shutil.move(str(src), str(dst))
    logger.info("archive_evolved_cohort: 已将 %s 降级为 %s", src, dst)
    return str(dst)


def list_archived_cohorts(archive_dir: str) -> List[str]:
    """列出所有版本化 archive 的 evolved cohort 文件。

    Parameters
    ----------
    archive_dir : str
        archive 目录路径。

    Returns
    -------
    List[str]
        archive 文件路径列表，按版本号排序。
    """
    p = Path(archive_dir)
    if not p.exists():
        return []

    archives = sorted(
        p.glob("archive_evolved_candidates_v*.json"),
        key=lambda f: f.name,
    )
    return [str(f) for f in archives]
