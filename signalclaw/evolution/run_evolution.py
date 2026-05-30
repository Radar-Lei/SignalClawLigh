"""run_evolution.py - 进化主脚本。

运行所有路口的 GLM 离线进化流程。

三种运行模式：
1. deployable 模式（默认，--require-sumo-for-champion）：
   - 必须有 SUMO evaluator，否则直接报错
   - 使用 SealedSUMOEvaluator 做 paired evaluation
   - 只有通过非退化门槛的候选才会写入 evolved_cohort.json

2. archive-only 模式（--archive-only）：
   - 不要求 SUMO evaluator，可以没有
   - 候选只保存到 archive，不写 deployable evolved_cohort.json
   - 适合开发调试、离线分析

3. tournament 模式（--tournament）：
   - 使用 SealedTournament 候选池+锦标赛进化
   - GLM 每轮生成 30 个候选，经过 AST/behavior/replay/micro-SUMO/paired-SUMO
     多级筛选，只有通过 non-degradation gate 的才能成为 champion
   - 输出 tournament_stats.json 记录每个路口的 TournamentResult

4. tournament + cohort-search 模式（--tournament --cohort-search）：
   - 在 tournament 完成后，自动运行全网 cohort 组合搜索
   - 验证 champion 组合在全网尺度上不退化（防止局部变好推拥堵给下游）
   - 支持 greedy 和 beam 两种搜索策略

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

    # tournament 模式
    python -m signalclaw.evolution.run_evolution \\
        --cohort artifacts/skills/cohorts/seed_cohort.json \\
        --archive-dir artifacts/evolution_archive \\
        --tournament \\
        --tournament-candidates 30 \\
        --tournament-rounds 3

    # tournament + cohort-search 模式（全网组合搜索）
    python -m signalclaw.evolution.run_evolution \\
        --cohort artifacts/skills/cohorts/seed_cohort.json \\
        --archive-dir artifacts/evolution_archive \\
        --tournament \\
        --cohort-search \\
        --search-strategy greedy
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
from signalclaw.evolution.dsl_compiler import DslCompiler
from signalclaw.evolution.evaluator_replay import ReplayEvaluator
from signalclaw.evolution.feature_mask import DEFAULT_FEATURE_MASK, FeatureMask
from signalclaw.evolution.glm_mutator import GLMSkillMutator
from signalclaw.evolution.per_intersection import PerIntersectionEvolver
from signalclaw.evolution.prompt_builder import PromptBuilder
from signalclaw.evolution.selector import SkillSelector
from signalclaw.evolution.tournament import SealedTournament, TournamentConfig, TournamentResult
from signalclaw.evolution.cohort_search import (
    CohortSearch, CohortSearchConfig, CohortSearchResult,
    build_candidates_from_tournament, save_champion_cohort,
)
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
    tournament: bool = False,
    tournament_candidates: int = 30,
    tournament_rounds: int = 3,
    tournament_top_k_micro: int = 10,
    tournament_top_k_paired: int = 3,
    tournament_micro_duration: float = 600.0,
    tournament_full_duration: float = 3600.0,
    cohort_search: bool = False,
    cohort_search_strategy: str = "greedy",
    cohort_search_beam_width: int = 3,
    cohort_search_threshold: float = 0.02,
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

    # ---- 路由到 tournament 模式 ----
    if tournament:
        return _run_tournament_mode(
            cohort=cohort,
            archive_dir=archive_dir,
            glm_mutator=glm_mutator,
            prompt_builder=prompt_builder,
            ast_sandbox=ast_sandbox,
            archive=archive,
            selector=selector,
            sumo_evaluator=sumo_evaluator,
            network_constraints=network_constraints,
            sql_profile=sql_profile,
            feature_mask=feature_mask,
            crossing_filter=crossing_filter,
            phase_count=phase_count,
            tournament_candidates=tournament_candidates,
            tournament_rounds=tournament_rounds,
            tournament_top_k_micro=tournament_top_k_micro,
            tournament_top_k_paired=tournament_top_k_paired,
            tournament_micro_duration=tournament_micro_duration,
            tournament_full_duration=tournament_full_duration,
            cohort_search=cohort_search,
            cohort_search_strategy=cohort_search_strategy,
            cohort_search_beam_width=cohort_search_beam_width,
            cohort_search_threshold=cohort_search_threshold,
            scenario_catalog=scenario_catalog,
        )

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


def _run_tournament_mode(
    cohort: SkillCohort,
    archive_dir: str,
    glm_mutator: GLMSkillMutator,
    prompt_builder: PromptBuilder,
    ast_sandbox: ASTSandbox,
    archive: SkillArchive,
    selector: SkillSelector,
    sumo_evaluator,
    network_constraints: NetworkConstraints,
    sql_profile: Optional[SQLReferenceProfile],
    feature_mask: FeatureMask,
    crossing_filter: Optional[list],
    phase_count: int,
    tournament_candidates: int,
    tournament_rounds: int,
    tournament_top_k_micro: int,
    tournament_top_k_paired: int,
    tournament_micro_duration: float,
    tournament_full_duration: float,
    cohort_search: bool = False,
    cohort_search_strategy: str = "greedy",
    cohort_search_beam_width: int = 3,
    cohort_search_threshold: float = 0.02,
    scenario_catalog=None,
) -> Dict:
    """Tournament 模式：使用 SealedTournament 候选池+锦标赛进化。"""
    print("\n[evolution] ========== TOURNAMENT 模式 ==========")
    print(f"[evolution] candidates_per_round={tournament_candidates}")
    print(f"[evolution] max_rounds={tournament_rounds}")
    print(f"[evolution] top_k_micro={tournament_top_k_micro}, top_k_paired={tournament_top_k_paired}")
    print(f"[evolution] micro_duration={tournament_micro_duration}s, full_duration={tournament_full_duration}s")

    dsl_compiler = DslCompiler(feature_mask=feature_mask)

    tournament_config = TournamentConfig(
        candidates_per_round=tournament_candidates,
        micro_sim_duration=tournament_micro_duration,
        full_sim_duration=tournament_full_duration,
        max_rounds=tournament_rounds,
        top_k_micro=tournament_top_k_micro,
        top_k_paired=tournament_top_k_paired,
        use_dsl=True,
    )

    crossing_ids = sorted(cohort.skills.keys())
    if crossing_filter:
        crossing_ids = [cid for cid in crossing_ids if cid in crossing_filter]

    # 每个路口、每种 skill 类型的 tournament 结果
    all_tournament_results: Dict[str, dict] = {}
    # champion 跟踪（用于构建 evolved cohort）
    champions: Dict[str, dict] = {}

    for idx, crossing_id in enumerate(crossing_ids):
        print(f"\n[evolution] === 路口 {crossing_id} ({idx + 1}/{len(crossing_ids)}) ===")

        skill_dirs = cohort.skills[crossing_id]
        cycle_dir = skill_dirs.get("cycle", "")
        phase_dir = skill_dirs.get("phase", "")

        if not cycle_dir or not phase_dir:
            print(f"[evolution] 跳过 {crossing_id}: 缺少 cycle 或 phase skill")
            continue

        try:
            seed_cycle_code = load_skill_code(cycle_dir)
            seed_phase_code = load_skill_code(phase_dir)
        except FileNotFoundError as e:
            print(f"[evolution] 跳过 {crossing_id}: {e}")
            continue

        inferred_phase_count = _infer_phase_count(seed_cycle_code, phase_count)
        constraints = network_constraints.get(crossing_id)
        replay_evaluator = ReplayEvaluator(constraints)

        crossing_results = {}
        incumbent_cycle_code = seed_cycle_code
        incumbent_phase_code = seed_phase_code

        for skill_type in ("cycle", "phase"):
            print(f"\n[evolution] --- {crossing_id}/{skill_type} tournament ---")

            paired_code = incumbent_cycle_code if skill_type == "phase" else None

            tournament = SealedTournament(
                crossing_id=crossing_id,
                skill_type=skill_type,
                config=tournament_config,
                glm_mutator=glm_mutator,
                prompt_builder=prompt_builder,
                ast_sandbox=ast_sandbox,
                replay_evaluator=replay_evaluator,
                archive=archive,
                selector=selector,
                constraints=constraints,
                phase_count=inferred_phase_count,
                dsl_compiler=dsl_compiler,
                sumo_evaluator=sumo_evaluator,
                cohort=cohort,
                sql_profile=sql_profile,
                feature_mask=feature_mask,
                paired_skill_code=paired_code,
            )

            seed_code = seed_cycle_code if skill_type == "cycle" else seed_phase_code
            incumbent_code = (
                incumbent_cycle_code
                if skill_type == "cycle"
                else incumbent_phase_code
            )

            # 多轮 tournament
            best_result = None
            for round_num in range(1, tournament_rounds + 1):
                print(
                    f"[tournament] {crossing_id}/{skill_type} "
                    f"round {round_num}/{tournament_rounds}"
                )
                try:
                    result = tournament.run(
                        seed_code=seed_code,
                        incumbent_code=incumbent_code,
                        round_num=round_num,
                    )
                    best_result = result

                    if result.champion_id:
                        print(
                            f"[tournament] round {round_num} champion: "
                            f"{result.champion_id} (score={result.champion_score})"
                        )
                        # 更新 incumbent 为 champion
                        champion_entry = tournament.incumbent
                        if champion_entry and champion_entry.code:
                            incumbent_code = champion_entry.code
                    else:
                        print(
                            f"[tournament] round {round_num}: 无新 champion，保持 incumbent"
                        )

                except Exception as e:
                    print(f"[tournament] round {round_num} 异常: {e}")
                    break

            if best_result is not None:
                crossing_results[skill_type] = best_result.to_dict()

                # 记录 champion entry
                champion_entry = tournament.incumbent
                if champion_entry and champion_entry.accepted_for_deployment:
                    if skill_type == "cycle":
                        incumbent_cycle_code = champion_entry.code
                    else:
                        incumbent_phase_code = champion_entry.code

        # 保存路口的 tournament 统计
        all_tournament_results[crossing_id] = crossing_results

        # 记录 champion 信息用于 evolved cohort
        champions[crossing_id] = {
            "cycle_code": incumbent_cycle_code,
            "phase_code": incumbent_phase_code,
            "cycle_is_seed": incumbent_cycle_code == seed_cycle_code,
            "phase_is_seed": incumbent_phase_code == seed_phase_code,
        }

    # ---- 保存 tournament_stats.json ----
    stats_path = os.path.join(archive_dir, "tournament_stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(all_tournament_results, f, indent=2, ensure_ascii=False)
    print(f"\n[evolution] Tournament stats 保存到: {stats_path}")

    # ---- 保存 evolved cohort ----
    evolved_skills = {}
    all_accepted = True
    for crossing_id, champ_info in champions.items():
        cycle_is_seed = champ_info["cycle_is_seed"]
        phase_is_seed = champ_info["phase_is_seed"]

        if cycle_is_seed and phase_is_seed:
            # 两个都是 seed，使用原始 skill 目录
            evolved_skills[crossing_id] = cohort.skills[crossing_id]
            all_accepted = False
        else:
            # 至少一个有进化结果，保存 evolved skill
            # 使用 archive 中已有的 _save_evolved_skill 逻辑
            cycle_entry = archive.get_best(crossing_id, "cycle")
            phase_entry = archive.get_best(crossing_id, "phase")

            cycle_dir_out = _save_evolved_skill(
                archive_dir, crossing_id, "cycle",
                cycle_entry if cycle_entry and cycle_entry.accepted_for_deployment else None,
            )
            phase_dir_out = _save_evolved_skill(
                archive_dir, crossing_id, "phase",
                phase_entry if phase_entry and phase_entry.accepted_for_deployment else None,
            )

            if cycle_dir_out and phase_dir_out:
                evolved_skills[crossing_id] = {
                    "cycle": cycle_dir_out,
                    "phase": phase_dir_out,
                }
            else:
                evolved_skills[crossing_id] = cohort.skills[crossing_id]
                all_accepted = False

    has_any_deployable = any(
        not info["cycle_is_seed"] or not info["phase_is_seed"]
        for info in champions.values()
    )
    cohort_source = "sealed_tournament_champion" if has_any_deployable else "seed_fallback"

    evolved_cohort = SkillCohort(
        cohort_id=f"tournament_{cohort.cohort_id}",
        skills=evolved_skills,
        frozen=True,
        glm_used_online=False,
        exploration=False,
        created_by="run_evolution.py (tournament mode)",
        source=cohort_source,
        all_skills_accepted_for_deployment=all_accepted,
    )
    evolved_cohort_path = os.path.join(archive_dir, "evolved_cohort.json")
    evolved_cohort.save(evolved_cohort_path)
    print(f"[evolution] Evolved cohort 保存到: {evolved_cohort_path}")

    # ---- 保存摘要 ----
    archive.save()
    summary_path = os.path.join(archive_dir, "evolution_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(all_tournament_results, f, indent=2, ensure_ascii=False)
    print(f"[evolution] 进化摘要保存到: {summary_path}")

    total = len(all_tournament_results)
    total_candidates = sum(
        r.get("cycle", {}).get("candidate_count", 0) +
        r.get("phase", {}).get("candidate_count", 0)
        for r in all_tournament_results.values()
        if isinstance(r, dict)
    )
    total_champions = sum(
        r.get("cycle", {}).get("accepted_champion_count", 0) +
        r.get("phase", {}).get("accepted_champion_count", 0)
        for r in all_tournament_results.values()
        if isinstance(r, dict)
    )
    print(f"\n[evolution] Tournament 完成: {total} 个路口")
    print(f"[evolution] 总候选数: {total_candidates}")
    print(f"[evolution] 总 champion 数: {total_champions}")
    print(f"[evolution] Archive 总条目: {archive.count()}")

    # ---- Cohort search（可选） ----
    if cohort_search and total_champions > 0:
        _run_cohort_search(
            seed_cohort=cohort,
            evolved_cohort=evolved_cohort,
            tournament_stats=all_tournament_results,
            archive_dir=archive_dir,
            sumo_evaluator=sumo_evaluator,
            scenario_catalog=scenario_catalog,
            strategy=cohort_search_strategy,
            beam_width=cohort_search_beam_width,
            degradation_threshold=cohort_search_threshold,
            sim_duration=tournament_full_duration,
            seed=42,
        )

    return all_tournament_results


def _run_cohort_search(
    seed_cohort: SkillCohort,
    evolved_cohort: SkillCohort,
    tournament_stats: Dict,
    archive_dir: str,
    sumo_evaluator=None,
    scenario_catalog=None,
    strategy: str = "greedy",
    beam_width: int = 3,
    degradation_threshold: float = 0.02,
    sim_duration: float = 3600.0,
    seed: int = 42,
) -> Optional[CohortSearchResult]:
    """在 tournament 完成后运行全网 cohort 组合搜索。

    Parameters
    ----------
    seed_cohort : SkillCohort
        seed cohort。
    evolved_cohort : SkillCohort
        进化后的 cohort（包含 champion skill）。
    tournament_stats : Dict
        tournament_stats.json 内容。
    archive_dir : str
        archive 目录路径。
    sumo_evaluator : optional
        SUMO evaluator（用于查找 sumocfg 路径）。
    scenario_catalog : optional
        场景目录。
    strategy : str
        搜索策略 "greedy" 或 "beam"。
    beam_width : int
        beam search 宽度。
    degradation_threshold : float
        退化阈值。
    sim_duration : float
        全网仿真时长。
    seed : int
        随机种子。

    Returns
    -------
    CohortSearchResult or None
        搜索结果，如果没有 candidate 或无法创建则返回 None。
    """
    print(f"\n[evolution] ========== COHORT SEARCH ==========")
    print(f"[evolution] 策略: {strategy}, beam_width: {beam_width}")

    # 构建 candidates
    candidates = build_candidates_from_tournament(
        tournament_stats=tournament_stats,
        seed_cohort=seed_cohort,
        evolved_cohort=evolved_cohort,
        archive_dir=archive_dir,
    )

    if not candidates:
        print("[evolution] 无 champion 候选，跳过 cohort search")
        return None

    print(f"[evolution] 找到 {len(candidates)} 个 champion 候选路口")

    # 查找 sumocfg 路径
    sumocfg_path = None
    if scenario_catalog is not None:
        for entry in scenario_catalog:
            if entry.sumocfg_file and os.path.exists(entry.sumocfg_file):
                sumocfg_path = entry.sumocfg_file
                break

    if sumocfg_path is None and sumo_evaluator is not None:
        sumocfg_path = getattr(
            sumo_evaluator, "_sumocfg_path",
            getattr(sumo_evaluator, "sumocfg_path", None),
        )

    if sumocfg_path is None:
        # fallback: 尝试默认路径
        project_root = os.path.abspath(os.path.join(archive_dir, "..", ".."))
        candidate_paths = [
            os.path.join(project_root, "sumo_scenarios", "chengdu", "chengdu.sumocfg"),
        ]
        for p in candidate_paths:
            if os.path.exists(p):
                sumocfg_path = p
                break

    if sumocfg_path is None:
        print("[evolution] 无法找到 sumocfg 文件，跳过 cohort search")
        return None

    # 查找 neighbor graph 路径
    project_root = os.path.abspath(os.path.join(archive_dir, "..", ".."))
    neighbor_graph_path = os.path.join(
        project_root, "artifacts", "topology", "one_hop_neighbors.json"
    )

    # 创建搜索配置
    config = CohortSearchConfig(
        beam_width=beam_width,
        network_sim_duration=sim_duration,
        degradation_threshold=degradation_threshold,
        seed=seed,
        strategy=strategy,
    )

    # 创建搜索器并执行
    searcher = CohortSearch(
        config=config,
        candidates=candidates,
        seed_cohort=seed_cohort,
        evolved_cohort=evolved_cohort,
        sumocfg_path=sumocfg_path,
        neighbor_graph_path=neighbor_graph_path,
    )

    result = searcher.search()

    # 保存结果
    result_path = os.path.join(archive_dir, "cohort_search_result.json")
    result.save(result_path)
    print(f"[evolution] Cohort search 结果保存到: {result_path}")

    # 保存最终 champion cohort
    champion_cohort_path = os.path.join(archive_dir, "final_champion_cohort.json")
    save_champion_cohort(
        result=result,
        seed_cohort=seed_cohort,
        evolved_cohort=evolved_cohort,
        output_path=champion_cohort_path,
    )
    print(f"[evolution] 最终 champion cohort 保存到: {champion_cohort_path}")

    return result



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

    # Tournament 模式参数
    parser.add_argument(
        "--tournament",
        action="store_true",
        default=False,
        help="使用 SealedTournament 候选池+锦标赛进化模式。"
             "GLM 每轮生成大量候选，经过多级筛选后只有通过 non-degradation gate "
             "的才能成为 champion。",
    )
    parser.add_argument(
        "--tournament-candidates",
        type=int,
        default=30,
        help="Tournament 模式下每轮生成的候选数量（默认 30）",
    )
    parser.add_argument(
        "--tournament-rounds",
        type=int,
        default=3,
        help="Tournament 模式下最大进化轮数（默认 3）",
    )
    parser.add_argument(
        "--tournament-top-k-micro",
        type=int,
        default=10,
        help="Tournament 模式下 micro-SUMO 快筛后保留的 top-k（默认 10）",
    )
    parser.add_argument(
        "--tournament-top-k-paired",
        type=int,
        default=3,
        help="Tournament 模式下 paired-SUMO 后保留的 top-k（默认 3）",
    )
    parser.add_argument(
        "--tournament-micro-duration",
        type=float,
        default=600.0,
        help="Tournament 模式下 micro-SUMO 快筛时长（秒，默认 600）",
    )
    parser.add_argument(
        "--tournament-full-duration",
        type=float,
        default=3600.0,
        help="Tournament 模式下 full-SUMO 完整评估时长（秒，默认 3600）",
    )

    # Cohort search 参数
    parser.add_argument(
        "--cohort-search",
        action="store_true",
        default=False,
        help="Tournament 完成后运行全网 cohort 组合搜索，"
             "验证 cohort 组合在全网尺度上不退化。",
    )
    parser.add_argument(
        "--search-strategy",
        choices=["greedy", "beam"],
        default="greedy",
        help="Cohort search 搜索策略（默认 greedy）",
    )
    parser.add_argument(
        "--search-beam-width",
        type=int,
        default=3,
        help="Beam search 宽度（默认 3，仅 beam 策略有效）",
    )
    parser.add_argument(
        "--search-degradation-threshold",
        type=float,
        default=0.02,
        help="允许全网退化的比例（默认 0.02 即 2%%）",
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
        tournament=args.tournament,
        tournament_candidates=args.tournament_candidates,
        tournament_rounds=args.tournament_rounds,
        tournament_top_k_micro=args.tournament_top_k_micro,
        tournament_top_k_paired=args.tournament_top_k_paired,
        tournament_micro_duration=args.tournament_micro_duration,
        tournament_full_duration=args.tournament_full_duration,
        cohort_search=args.cohort_search,
        cohort_search_strategy=args.search_strategy,
        cohort_search_beam_width=args.search_beam_width,
        cohort_search_threshold=args.search_degradation_threshold,
    )


if __name__ == "__main__":
    main()
