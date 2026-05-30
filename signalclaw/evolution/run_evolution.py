"""run_evolution.py - 进化主脚本。

运行所有路口的 GLM 离线进化流程。

两种运行模式：
1. deployable 模式（默认，--require-sumo-for-champion）：
   - 必须有 SUMO evaluator，否则直接报错
   - 使用 SealedSUMOEvaluator 做 paired evaluation
   - 只有通过非退化门槛的候选才会写入 evolved_cohort.json

2. archive-only 模式（--archive-only）：
   - 不要求 SUMO evaluator，可以没有
   - 候选只保存到 archive，不写 deployable evolved_cohort.json
   - 适合开发调试、离线分析

用法:
    # deployable 模式（默认）
    python -m signalclaw.evolution.run_evolution \\
        --cohort artifacts/skills/cohorts/seed_cohort.json \\
        --archive-dir artifacts/evolution_archive \\
        --n-candidates 3 \\
        --max-rounds 2

    # archive-only 模式
    python -m signalclaw.evolution.run_evolution \\
        --cohort artifacts/skills/cohorts/seed_cohort.json \\
        --archive-dir artifacts/evolution_archive \\
        --archive-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional

# 确保项目根目录在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from signalclaw.core.constraints import IntersectionConstraints, NetworkConstraints
from signalclaw.evolution.archive import ArchiveEntry, SkillArchive
from signalclaw.evolution.ast_sandbox import ASTSandbox
from signalclaw.evolution.evaluator_replay import ReplayEvaluator
from signalclaw.evolution.feature_mask import DEFAULT_FEATURE_MASK, FeatureMask
from signalclaw.evolution.glm_mutator import GLMSkillMutator
from signalclaw.evolution.per_intersection import PerIntersectionEvolver
from signalclaw.evolution.prompt_builder import PromptBuilder
from signalclaw.evolution.selector import SkillSelector
from signalclaw.reference.profile_schema import SQLReferenceProfile
from signalclaw.reference.sql_profiler import SQLReferenceProfiler
from signalclaw.skills.artifact import SkillArtifact
from signalclaw.skills.cohort import SkillCohort


def load_seed_cohort(cohort_path: str) -> SkillCohort:
    """加载 seed cohort。"""
    return SkillCohort.load(cohort_path)


def load_skill_code(artifact_dir: str) -> str:
    """从 artifact 目录加载 skill.py 的代码。"""
    skill_path = Path(artifact_dir) / "skill.py"
    if not skill_path.exists():
        raise FileNotFoundError(f"skill.py not found in {artifact_dir}")
    return skill_path.read_text(encoding="utf-8")


def build_network_constraints(
    cohort: SkillCohort,
    default_constraints: Optional[IntersectionConstraints] = None,
) -> NetworkConstraints:
    """从 cohort 构建 NetworkConstraints。

    如果没有路口特定的约束配置，使用默认值。
    """
    if default_constraints is None:
        default_constraints = IntersectionConstraints()

    intersections = {}
    for crossing_id in cohort.skills:
        intersections[crossing_id] = IntersectionConstraints(
            min_green=default_constraints.min_green,
            max_green=default_constraints.max_green,
            min_cycle=default_constraints.min_cycle,
            max_cycle=default_constraints.max_cycle,
            yellow_time=default_constraints.yellow_time,
            all_red_time=default_constraints.all_red_time,
            max_extend=default_constraints.max_extend,
            max_shorten=default_constraints.max_shorten,
        )

    return NetworkConstraints(intersections=intersections)


def _try_create_sumo_evaluator(scenario_catalog, network_constraints):
    """尝试创建 SealedSUMOEvaluator，失败时 graceful fallback 到 None。

    返回 SealedSUMOEvaluator 实例（用于 deployable 模式的 paired evaluation），
    或者 None（archive-only 模式可以接受 None）。

    SealedSUMOEvaluator 需要三个关键依赖：
    1. scenario_catalog 中至少有一个带有有效 sumocfg_file 的场景
    2. 从 sumocfg 或 net.xml 构建 NeighborGraph
    3. SUMO/TraCI 可用

    任何一个条件不满足时，安全返回 None。
    """
    if scenario_catalog is None:
        print("[evolution] 无场景目录，跳过 SUMO 评估器创建")
        return None

    try:
        from signalclaw.evolution.evaluator_sumo import SealedSUMOEvaluator
        from signalclaw.network.neighbor_graph import NeighborGraph
    except ImportError as e:
        print(f"[evolution] SUMO 依赖未安装，跳过 SUMO 评估: {e}")
        return None

    # 查找第一个有效的 sumocfg 文件
    sumocfg_path = None
    for entry in scenario_catalog:
        if entry.sumocfg_file and os.path.exists(entry.sumocfg_file):
            sumocfg_path = entry.sumocfg_file
            break

    if sumocfg_path is None:
        print("[evolution] 场景目录中无有效的 sumocfg 文件，跳过 SUMO 评估器创建")
        return None

    # 尝试从 net.xml 构建 NeighborGraph
    neighbor_graph = None
    try:
        # 从 sumocfg 中提取 net_file 路径
        import xml.etree.ElementTree as ET
        tree = ET.parse(sumocfg_path)
        root = tree.getroot()
        net_file = None
        for input_elem in root.iter("input"):
            for net_elem in input_elem.iter("net-file"):
                net_file = net_elem.get("value")
                break

        if net_file:
            # net_file 可能是相对路径，需要相对于 sumocfg 所在目录解析
            if not os.path.isabs(net_file):
                net_file = os.path.join(os.path.dirname(sumocfg_path), net_file)

            if os.path.exists(net_file):
                neighbor_graph = NeighborGraph.from_sumo_net(net_file)
                print(f"[evolution] 从 {net_file} 构建邻居拓扑图成功")
            else:
                print(f"[evolution] net.xml 文件不存在: {net_file}")
    except Exception as e:
        print(f"[evolution] 构建邻居拓扑图失败: {e}")

    if neighbor_graph is None:
        # 创建空的 NeighborGraph 作为 fallback
        neighbor_graph = NeighborGraph()
        print("[evolution] 使用空邻居拓扑图（无路网拓扑信息）")

    try:
        evaluator = SealedSUMOEvaluator(
            sumocfg_path=sumocfg_path,
            neighbor_graph=neighbor_graph,
            constraints=network_constraints,
        )
        print(f"[evolution] SealedSUMO 评估器创建成功（sumocfg={sumocfg_path}）")
        return evaluator
    except Exception as e:
        print(f"[evolution] SealedSUMO 评估器创建失败: {e}")
        return None


def run_evolution(
    cohort_path: str,
    archive_dir: str,
    n_candidates: int = 3,
    max_rounds: int = 2,
    phase_count: int = 4,
    temperature: float = 0.5,
    max_tokens: int = 16384,
    crossing_filter: Optional[list] = None,
    sql_profile_path: Optional[str] = None,
    scenario_catalog_path: Optional[str] = None,
    archive_only: bool = False,
) -> Dict:
    """运行所有路口的进化。

    Parameters
    ----------
    cohort_path : str
        seed cohort JSON 路径
    archive_dir : str
        进化 archive 目录
    n_candidates : int
        每轮生成候选数
    max_rounds : int
        最大进化轮数
    phase_count : int
        默认相位数（如无法从代码推断）
    temperature : float
        GLM temperature
    max_tokens : int
        GLM max_tokens
    crossing_filter : list, optional
        只进化指定的路口 ID（用于调试）
    sql_profile_path : str, optional
        SQL 参考画像 JSON 路径。如果提供，会在 GLM prompt 中注入先验信息，
        并在 AST 检查后执行 Prior Consistency Check。
    scenario_catalog_path : str, optional
        场景目录 JSON 路径。如果提供，SUMO 评估将使用多场景评估模式。
    archive_only : bool
        如果为 True，只保存候选到 archive，不要求 SUMO sealed evaluation，
        也不写 deployable evolved_cohort.json。如果为 False（默认），必须有
        SUMO sealed evaluation 才能写 evolved_cohort.json。

    Returns
    -------
    Dict
        进化结果摘要
    """
    # ---- 1. 加载 seed cohort ----
    print(f"[evolution] 加载 seed cohort: {cohort_path}")
    cohort = load_seed_cohort(cohort_path)
    print(f"[evolution] 共 {len(cohort.skills)} 个路口")

    # ---- 2. 加载 SQL 参考画像 ----
    sql_profile: Optional[SQLReferenceProfile] = None
    if sql_profile_path and os.path.exists(sql_profile_path):
        print(f"[evolution] 加载 SQL 参考画像: {sql_profile_path}")
        sql_profile = SQLReferenceProfile.load(sql_profile_path)
    elif sql_profile_path:
        print(f"[evolution] SQL 画像文件不存在，使用默认先验: {sql_profile_path}")
        profiler = SQLReferenceProfiler()
        sql_profile = profiler.build_profile()
    else:
        # 即使没有指定路径，也构建默认画像
        profiler = SQLReferenceProfiler()
        sql_profile = profiler.build_profile()
        print("[evolution] 使用默认 SQL 参考画像")

    # ---- 3. 加载场景目录（如果提供） ----
    scenario_catalog = None
    if scenario_catalog_path and os.path.exists(scenario_catalog_path):
        from signalclaw.scenario.scenario_catalog import ScenarioCatalog
        print(f"[evolution] 加载场景目录: {scenario_catalog_path}")
        scenario_catalog = ScenarioCatalog.load(scenario_catalog_path)
        print(f"[evolution] 场景目录包含 {len(scenario_catalog)} 个场景")

    # ---- 4. 构建约束 ----
    network_constraints = build_network_constraints(cohort)

    # ---- 5. 初始化组件 ----
    # 创建统一的 FeatureMask（所有组件共享同一配置）
    feature_mask = FeatureMask()
    disabled = feature_mask.get_disabled_feature_names()
    if disabled:
        print(f"[evolution] Feature Mask: 以下特征不可用: {', '.join(disabled)}")

    glm_mutator = GLMSkillMutator(
        temperature=temperature,
        max_tokens=max_tokens,
    )
    prompt_builder = PromptBuilder(sql_profile=sql_profile, feature_mask=feature_mask)
    ast_sandbox = ASTSandbox(feature_mask=feature_mask)
    archive = SkillArchive(archive_dir)

    # 创建 SealedSUMOEvaluator
    # deployable 模式下必须有 SUMO evaluator，否则直接报错
    # archive-only 模式下可以没有
    sumo_evaluator = _try_create_sumo_evaluator(scenario_catalog, network_constraints)

    if not archive_only and sumo_evaluator is None:
        raise RuntimeError(
            "Deployable 模式需要 SUMO evaluator，但创建失败。"
            "请检查：(1) --scenario-catalog 是否提供且路径正确；"
            "(2) 场景目录中是否有有效的 .sumocfg 文件；"
            "(3) SUMO/TraCI 是否已安装。"
            "如果只是想保存候选到 archive 进行分析，请使用 --archive-only 模式。"
        )

    selector = SkillSelector(sumo_evaluator=sumo_evaluator)

    # ---- 6. 对每个路口运行进化 ----
    results = {}
    crossing_ids = sorted(cohort.skills.keys())

    if crossing_filter:
        crossing_ids = [cid for cid in crossing_ids if cid in crossing_filter]

    for idx, crossing_id in enumerate(crossing_ids):
        print(f"\n[evolution] === 路口 {crossing_id} ({idx + 1}/{len(crossing_ids)}) ===")

        skill_dirs = cohort.skills[crossing_id]
        cycle_dir = skill_dirs.get("cycle", "")
        phase_dir = skill_dirs.get("phase", "")

        if not cycle_dir or not phase_dir:
            print(f"[evolution] 跳过 {crossing_id}: 缺少 cycle 或 phase skill")
            continue

        # 加载 seed 代码
        try:
            seed_cycle_code = load_skill_code(cycle_dir)
            seed_phase_code = load_skill_code(phase_dir)
        except FileNotFoundError as e:
            print(f"[evolution] 跳过 {crossing_id}: {e}")
            continue

        # 推断相位数
        inferred_phase_count = _infer_phase_count(seed_cycle_code, phase_count)

        # 创建评估器
        constraints = network_constraints.get(crossing_id)
        replay_evaluator = ReplayEvaluator(constraints)

        # 创建进化器（传入 SQL 参考画像以启用 Prior Consistency Check）
        evolver = PerIntersectionEvolver(
            crossing_id=crossing_id,
            glm_mutator=glm_mutator,
            prompt_builder=prompt_builder,
            ast_sandbox=ast_sandbox,
            replay_evaluator=replay_evaluator,
            sumo_evaluator=sumo_evaluator,
            archive=archive,
            selector=selector,
            constraints=constraints,
            phase_count=inferred_phase_count,
            sql_profile=sql_profile,
            cohort=cohort,
            feature_mask=feature_mask,
        )

        # 运行进化
        try:
            evolve_result = evolver.evolve(
                seed_cycle_code=seed_cycle_code,
                seed_phase_code=seed_phase_code,
                n_candidates=n_candidates,
                max_rounds=max_rounds,
            )

            cycle_best = evolve_result.get("cycle")
            phase_best = evolve_result.get("phase")

            result_summary = {
                "crossing_id": crossing_id,
                "cycle_best_id": cycle_best.candidate_id if cycle_best else None,
                "phase_best_id": phase_best.candidate_id if phase_best else None,
                "cycle_score": (
                    cycle_best.replay_report.get("score", 0.0)
                    if cycle_best and cycle_best.replay_report
                    else 0.0
                ),
                "phase_score": (
                    phase_best.replay_report.get("score", 0.0)
                    if phase_best and phase_best.replay_report
                    else 0.0
                ),
                # 部署状态字段
                "cycle_accepted_for_deployment": (
                    cycle_best.accepted_for_deployment
                    if cycle_best else False
                ),
                "phase_accepted_for_deployment": (
                    phase_best.accepted_for_deployment
                    if phase_best else False
                ),
                "cycle_has_real_sumo_report": (
                    cycle_best.has_real_sumo_report
                    if cycle_best else False
                ),
                "phase_has_real_sumo_report": (
                    phase_best.has_real_sumo_report
                    if phase_best else False
                ),
            }
            results[crossing_id] = result_summary

            print(
                f"[evolution] {crossing_id} 完成: "
                f"cycle_score={result_summary['cycle_score']:.4f}, "
                f"phase_score={result_summary['phase_score']:.4f}"
            )

        except Exception as e:
            print(f"[evolution] {crossing_id} 进化失败: {e}")
            results[crossing_id] = {
                "crossing_id": crossing_id,
                "error": str(e),
            }

    # ---- 5. 保存 evolved cohort ----
    if archive_only:
        # archive-only 模式：不写 deployable evolved_cohort.json
        print("\n[evolution] archive-only 模式，跳过 deployable evolved cohort 生成")
        _save_archive_only_cohort(cohort, archive_dir, results)
    else:
        # 默认模式（require-sumo-for-champion）：只有 accepted_for_deployment=true 才写入
        evolved_cohort = _build_evolved_cohort(
            cohort, archive, results
        )
        evolved_cohort_path = os.path.join(archive_dir, "evolved_cohort.json")
        evolved_cohort.save(evolved_cohort_path)
        print(f"\n[evolution] Evolved cohort 保存到: {evolved_cohort_path}")

    # ---- 6. 保存摘要 ----
    summary_path = os.path.join(archive_dir, "evolution_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"[evolution] 进化摘要保存到: {summary_path}")

    # ---- 7. 打印统计 ----
    archive.save()
    total = len(results)
    success = sum(1 for r in results.values() if "error" not in r)
    print(f"\n[evolution] 完成: {success}/{total} 个路口成功进化")
    print(f"[evolution] Archive 总条目: {archive.count()}")

    return results


def _infer_phase_count(code: str, default: int = 4) -> int:
    """从代码中推断相位数量（简单启发式）。"""
    # 尝试从 range(n) 或 range(0, n) 中推断
    import re
    matches = re.findall(r"range\(\s*(\d+)\s*\)", code)
    if matches:
        # 取最大的合理值
        nums = [int(m) for m in matches if 2 <= int(m) <= 12]
        if nums:
            return max(nums)
    return default


def _save_archive_only_cohort(
    seed_cohort: SkillCohort,
    archive_dir: str,
    results: Dict,
) -> None:
    """archive-only 模式下保存一个引用 seed/incumbent 的 cohort 文件。

    不要求 SUMO sealed evaluation，cohort 中所有 skill 都是 seed 回退。
    写入 archive_evolved_cohort.json（带 source=archive_only 元数据），
    不覆盖 evolved_cohort.json。
    """
    skills = {}
    for crossing_id, result in results.items():
        if crossing_id in seed_cohort.skills:
            skills[crossing_id] = seed_cohort.skills[crossing_id]

    cohort = SkillCohort(
        cohort_id=f"archive_only_{seed_cohort.cohort_id}",
        skills=skills,
        frozen=True,
        glm_used_online=False,
        exploration=False,
        created_by="run_evolution.py",
        source="archive_only",
        all_skills_accepted_for_deployment=False,
    )
    cohort_path = os.path.join(archive_dir, "archive_evolved_cohort.json")
    cohort.save(cohort_path)
    print(f"[evolution] archive-only cohort 保存到: {cohort_path}")


def _build_evolved_cohort(
    seed_cohort: SkillCohort,
    archive: SkillArchive,
    results: Dict,
) -> SkillCohort:
    """从进化结果构建 evolved cohort。

    只引用 accepted_for_deployment=true 的 Skill。
    如果某个路口的 cycle 或 phase 没有 accepted_for_deployment=true 的候选，
    则回退到 seed cohort 中对应的 skill。
    如果没有任何路口有 accepted_for_deployment=true 的候选，
    写入引用 seed/incumbent 的 cohort。
    """
    evolved_skills = {}
    all_accepted = True

    for crossing_id, result in results.items():
        if "error" in result:
            # 失败的路口使用 seed
            evolved_skills[crossing_id] = seed_cohort.skills[crossing_id]
            all_accepted = False
            continue

        cycle_best_id = result.get("cycle_best_id")
        phase_best_id = result.get("phase_best_id")

        cycle_entry = archive.get(cycle_best_id) if cycle_best_id else None
        phase_entry = archive.get(phase_best_id) if phase_best_id else None

        # 检查是否 accepted_for_deployment
        cycle_accepted = (
            cycle_entry is not None and cycle_entry.accepted_for_deployment
        )
        phase_accepted = (
            phase_entry is not None and phase_entry.accepted_for_deployment
        )

        # 构建 evolved skill 目录结构（仅 accepted 的才保存为 evolved skill）
        cycle_dir = _save_evolved_skill(
            archive.archive_dir, crossing_id, "cycle", cycle_entry
        ) if cycle_accepted else None
        phase_dir = _save_evolved_skill(
            archive.archive_dir, crossing_id, "phase", phase_entry
        ) if phase_accepted else None

        if cycle_dir and phase_dir:
            evolved_skills[crossing_id] = {
                "cycle": cycle_dir,
                "phase": phase_dir,
            }
        else:
            # 降级使用 seed
            evolved_skills[crossing_id] = seed_cohort.skills[crossing_id]
            all_accepted = False

    # 确定 cohort source 标签
    has_any_deployable = any(
        result.get("cycle_accepted_for_deployment", False)
        or result.get("phase_accepted_for_deployment", False)
        for result in results.values()
        if "error" not in result
    )
    cohort_source = "sealed_sumo_champion" if has_any_deployable else "seed_fallback"

    return SkillCohort(
        cohort_id=f"evolved_{seed_cohort.cohort_id}",
        skills=evolved_skills,
        frozen=True,
        glm_used_online=False,
        exploration=False,
        created_by="run_evolution.py",
        source=cohort_source,
        all_skills_accepted_for_deployment=all_accepted,
    )


def _save_evolved_skill(
    archive_dir: str,
    crossing_id: str,
    skill_type: str,
    entry: Optional[ArchiveEntry],
) -> Optional[str]:
    """将进化后的 skill 保存为标准 artifact 目录结构。

    manifest 中包含完整的部署证据字段，来自 ArchiveEntry 的部署状态硬字段。
    """
    if entry is None or not entry.code:
        return None

    from signalclaw.skills.artifact import SkillArtifact, SkillMetrics
    from datetime import datetime, timezone

    # 创建 artifact 目录
    version = entry.generation
    artifact_dir = Path(archive_dir) / "evolved_skills" / crossing_id / skill_type / f"v{version:04d}"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # 保存 skill.py
    (artifact_dir / "skill.py").write_text(entry.code, encoding="utf-8")

    # 创建 manifest：包含部署证据字段
    code_hash = SkillArtifact.compute_code_hash(entry.code)
    artifact = SkillArtifact(
        skill_id=f"tls_{crossing_id}_{skill_type}_v{version:04d}",
        crossing_id=crossing_id,
        skill_type=skill_type,
        version=version,
        parent_skill_ids=entry.parent_ids,
        code_hash=code_hash,
        prompt_hash=entry.prompt_hash,
        glm_model=entry.glm_model,
        created_at=datetime.now(timezone.utc).isoformat(),
        frozen=True,
        online_learning=False,
        exploration=False,
        constraints_profile="default",
        metrics=SkillMetrics(
            replay_score=entry.replay_report.get("score", 0.0) if entry.replay_report else 0.0,
            sumo_score=entry.sumo_report.get("score", 0.0) if entry.sumo_report else 0.0,
        ),
    )

    # 将 manifest 转为 dict 后注入部署证据字段
    manifest_dict = artifact.to_dict()
    manifest_dict.update({
        # 部署状态硬字段（来自 ArchiveEntry）
        "is_archive_candidate": entry.is_archive_candidate,
        "is_deployable_champion": entry.is_deployable_champion,
        "has_real_sumo_report": entry.has_real_sumo_report,
        "paired_eval_passed": entry.paired_eval_passed,
        "accepted_for_deployment": entry.accepted_for_deployment,
        "incumbent_skill_id": entry.incumbent_skill_id,
        "rejection_reason": entry.deployment_rejection_reason or entry.rejection_reason or "",
    })

    (artifact_dir / "manifest.json").write_text(
        json.dumps(manifest_dict, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return str(artifact_dir.resolve())


def main():
    parser = argparse.ArgumentParser(
        description="运行 GLM 离线进化流程"
    )
    parser.add_argument(
        "--cohort",
        default="artifacts/skills/cohorts/seed_cohort.json",
        help="Seed cohort JSON 路径",
    )
    parser.add_argument(
        "--archive-dir",
        default="artifacts/evolution_archive",
        help="进化 archive 目录",
    )
    parser.add_argument(
        "--n-candidates",
        type=int,
        default=3,
        help="每轮生成的候选数量",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=2,
        help="每种 Skill 的最大进化轮数",
    )
    parser.add_argument(
        "--phase-count",
        type=int,
        default=4,
        help="默认相位数",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.5,
        help="GLM temperature",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=16384,
        help="GLM max_tokens（GLM-5.1 需要较大值，因为推理 tokens 也计入）",
    )
    parser.add_argument(
        "--crossing",
        nargs="*",
        default=None,
        help="只进化指定的路口 ID（用于调试）",
    )
    parser.add_argument(
        "--sql-profile",
        default=None,
        help="SQL 参考画像 JSON 路径（如果不提供则使用默认先验）",
    )
    parser.add_argument(
        "--scenario-catalog",
        default=None,
        help="场景目录 JSON 路径（用于多场景评估）",
    )
    deploy_group = parser.add_mutually_exclusive_group()
    deploy_group.add_argument(
        "--archive-only",
        action="store_true",
        default=False,
        help="archive-only 模式：不需要 SUMO 评估，只保存候选到 archive，"
             "不写 deployable evolved_cohort.json",
    )
    deploy_group.add_argument(
        "--require-sumo-for-champion",
        action="store_true",
        default=True,
        help="默认模式：必须有 SUMO sealed evaluation 才能写 evolved_cohort.json。"
             "此参数为默认行为，无需显式指定。",
    )

    args = parser.parse_args()

    run_evolution(
        cohort_path=args.cohort,
        archive_dir=args.archive_dir,
        n_candidates=args.n_candidates,
        max_rounds=args.max_rounds,
        phase_count=args.phase_count,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        crossing_filter=args.crossing,
        sql_profile_path=args.sql_profile,
        scenario_catalog_path=args.scenario_catalog,
        archive_only=args.archive_only,
    )


if __name__ == "__main__":
    main()
