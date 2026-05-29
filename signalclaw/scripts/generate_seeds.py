"""
generate_seeds.py - 为每个 TLS 生成 seed cycle/phase skill artifacts。

用法:
    python -m signalclaw.scripts.generate_seeds [--net sumo_scenarios/chengdu/chengdu.net.xml] [--output-dir artifacts]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


def get_tls_ids(net_path: str) -> List[str]:
    tree = ET.parse(net_path)
    root = tree.getroot()
    tls_ids: List[str] = []
    seen = set()
    for tl in root.findall(".//tlLogic"):
        tid = tl.get("id")
        if tid and tid not in seen:
            tls_ids.append(tid)
            seen.add(tid)
    for junction in root.findall(".//junction"):
        if junction.get("type") == "traffic_light":
            jid = junction.get("id")
            if jid and jid not in seen:
                tls_ids.append(jid)
                seen.add(jid)
    return sorted(tls_ids)


def _cycle_seed_code(tls_id: str) -> str:
    template = '''\
"""Cycle planner seed for intersection __TLS_ID__."""
from collections import deque
from typing import Dict, List, Optional
from signalclaw.core.state import NetworkObservation, CyclePlan

_min_green = 10.0
_max_green = 60.0
_base_cycle = 80.0
_history_window = 5

_pressure_history: Dict[int, deque] = {}
_last_green_time: Dict[int, float] = {}


def _get_history(phase_id: int) -> deque:
    if phase_id not in _pressure_history:
        _pressure_history[phase_id] = deque(maxlen=_history_window)
    return _pressure_history[phase_id]


def plan(obs: "NetworkObservation") -> "CyclePlan":
    ego = obs.ego
    green_phases = sorted(ego.phases.keys())
    if not green_phases:
        return CyclePlan(cycle_length=_base_cycle, green_times={}, phase_order=[])

    scores = {}
    for gp in green_phases:
        phase_obs = ego.phases.get(gp)
        if phase_obs is None:
            scores[gp] = 0.0
            continue
        local_pressure = phase_obs.queue
        downstream_total = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0.0
        n_downstream = max(len(ego.downstream_queue), 1)
        avg_downstream = downstream_total / n_downstream
        spillback_risk = max(0, avg_downstream - 5.0) * 2.0
        arrival_prediction = phase_obs.waiting_time * 0.3
        last_served = _last_green_time.get(gp, 0)
        hunger_time = obs.timestamp - last_served
        hunger_bonus = min(hunger_time * 0.5, 15.0)
        score = (
            1.0 * local_pressure
            + 0.4 * arrival_prediction
            - 0.8 * spillback_risk
            + 0.6 * hunger_bonus
        )
        scores[gp] = score
        _get_history(gp).append(score)

    total_queue = sum(p.queue for p in ego.phases.values())
    if total_queue < 5:
        cycle_length = _base_cycle * 0.7
    elif total_queue > 50:
        cycle_length = _base_cycle * 1.3
    else:
        cycle_length = _base_cycle * (0.7 + 0.6 * (total_queue - 5) / 45.0)
    cycle_length = max(40.0, min(180.0, cycle_length))

    min_score = min(scores.values())
    shifted = {gp: max(s - min_score + 1.0, 0.1) for gp, s in scores.items()}

    for gp in green_phases:
        hist = _get_history(gp)
        if len(hist) > 1:
            avg_hist = sum(hist) / len(hist)
            shifted[gp] = 0.7 * shifted[gp] + 0.3 * max(avg_hist - min_score + 1.0, 0.1)

    total_score = sum(shifted.values())
    green_times = {}
    for gp in green_phases:
        if total_score > 0:
            gt = cycle_length * (shifted[gp] / total_score)
        else:
            gt = cycle_length / len(green_phases)
        green_times[gp] = max(_min_green, min(_max_green, gt))

    plan = CyclePlan(
        cycle_length=sum(green_times.values()),
        green_times=green_times,
        phase_order=green_phases,
    )
    return plan


def _reset():
    _pressure_history.clear()
    _last_green_time.clear()
'''
    return template.replace("__TLS_ID__", tls_id)


def _phase_seed_code(tls_id: str) -> str:
    template = '''\
"""Phase micro adjuster seed for intersection __TLS_ID__."""
from typing import Dict, Optional
from signalclaw.core.state import NetworkObservation, CyclePlan, PhaseCommand

_extend_threshold = 3.0
_max_extend = 5.0
_decision_interval = 5.0

_phase_index: int = 0
_phase_remaining: float = 0.0
_current_plan_hash: int = 0


def _plan_hash(plan: "CyclePlan") -> int:
    return hash((plan.cycle_length, tuple(sorted(plan.green_times.items())), tuple(plan.phase_order)))


def decide(obs: "NetworkObservation", plan: "CyclePlan") -> "PhaseCommand":
    global _phase_index, _phase_remaining, _current_plan_hash

    ego = obs.ego
    phase_order = plan.phase_order
    if not phase_order:
        return PhaseCommand(
            action="hold", next_phase_id=ego.current_phase_id,
            duration=_decision_interval, reason_code="no_phases",
        )

    ph = _plan_hash(plan)
    if _current_plan_hash != ph:
        _phase_index = 0
        _current_plan_hash = ph
        first_phase = phase_order[0]
        _phase_remaining = plan.green_times.get(first_phase, 15.0)
        return PhaseCommand(
            action="switch", next_phase_id=first_phase,
            duration=plan.green_times.get(first_phase, 15.0),
            reason_code="new_plan",
        )

    remaining = _phase_remaining

    if remaining <= 0:
        next_idx = (_phase_index + 1) % len(phase_order)
        next_phase = phase_order[next_idx]
        _phase_index = next_idx
        _phase_remaining = plan.green_times.get(next_phase, 15.0)
        return PhaseCommand(
            action="switch", next_phase_id=next_phase,
            duration=_phase_remaining, reason_code="phase_end",
        )

    current_phase = phase_order[_phase_index]
    phase_obs = ego.phases.get(current_phase)

    if phase_obs is not None and remaining <= 10.0:
        current_queue = phase_obs.queue
        downstream_risk = sum(ego.downstream_queue.values()) if ego.downstream_queue else 0
        if current_queue > _extend_threshold and downstream_risk < 10:
            extend = min(_max_extend, _decision_interval)
            _phase_remaining = remaining + extend - _decision_interval
            return PhaseCommand(
                action="extend", next_phase_id=current_phase,
                duration=_phase_remaining + _decision_interval,
                reason_code=f"extend_high_demand_q{current_queue:.0f}",
            )
        if current_queue < 1.0 and remaining > 5.0:
            _phase_remaining = 0
            next_idx = (_phase_index + 1) % len(phase_order)
            next_phase = phase_order[next_idx]
            _phase_index = next_idx
            _phase_remaining = plan.green_times.get(next_phase, 15.0)
            return PhaseCommand(
                action="switch", next_phase_id=next_phase,
                duration=_phase_remaining,
                reason_code=f"early_switch_empty_q{current_queue:.0f}",
            )

    _phase_remaining -= _decision_interval
    return PhaseCommand(
        action="hold", next_phase_id=current_phase,
        duration=_decision_interval, reason_code="continuing",
    )


def _reset():
    global _phase_index, _phase_remaining, _current_plan_hash
    _phase_index = 0
    _phase_remaining = 0.0
    _current_plan_hash = 0
'''
    return template.replace("__TLS_ID__", tls_id)


def _make_manifest(
    tls_id: str,
    skill_type: str,
    version: int,
    code_hash: str,
    prompt_hash: str,
    scenario_hash: str,
) -> dict:
    from signalclaw.skills.artifact import SkillArtifact, SkillMetrics

    now = datetime.now(timezone.utc).isoformat()
    artifact = SkillArtifact(
        skill_id=f"tls_{tls_id}_{skill_type}_v{version:04d}",
        crossing_id=tls_id,
        skill_type=skill_type,
        version=version,
        parent_skill_ids=[],
        code_hash=code_hash,
        prompt_hash=prompt_hash,
        data_split_hash="",
        sumo_scenario_hash=scenario_hash,
        glm_model="seed",
        created_at=now,
        frozen=True,
        online_learning=False,
        exploration=False,
        constraints_profile="default",
        metrics=SkillMetrics(),
    )
    return json.loads(artifact.to_json())


def _file_hash(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def generate_seeds(
    net_path: str = "sumo_scenarios/chengdu/chengdu.net.xml",
    output_dir: str = "artifacts",
) -> Tuple[int, str]:
    """为网络中每个 TLS 生成 seed artifacts，返回 (count, cohort_path)。"""

    tls_ids = get_tls_ids(net_path)
    scenario_hash = _file_hash(net_path)
    out = Path(output_dir)
    cohort_skills: Dict[str, Dict[str, str]] = {}

    for tls_id in tls_ids:
        for skill_type, code_fn in [("cycle", _cycle_seed_code), ("phase", _phase_seed_code)]:
            version_dir = out / "skills" / tls_id / skill_type / "v0000"
            version_dir.mkdir(parents=True, exist_ok=True)

            code = code_fn(tls_id)
            code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
            prompt_hash = hashlib.sha256(f"seed_{skill_type}_{tls_id}".encode("utf-8")).hexdigest()

            (version_dir / "skill.py").write_text(code, encoding="utf-8")

            manifest = _make_manifest(tls_id, skill_type, 0, code_hash, prompt_hash, scenario_hash)
            (version_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )

        cycle_dir = str((out / "skills" / tls_id / "cycle" / "v0000").resolve())
        phase_dir = str((out / "skills" / tls_id / "phase" / "v0000").resolve())
        cohort_skills[tls_id] = {"cycle": cycle_dir, "phase": phase_dir}

    # 写入 cohort
    cohort_dir = out / "skills" / "cohorts"
    cohort_dir.mkdir(parents=True, exist_ok=True)
    cohort_path = cohort_dir / "seed_cohort.json"

    cohort = {
        "cohort_id": "seed_v0",
        "skills": cohort_skills,
        "frozen": True,
        "glm_used_online": False,
        "created_by": "generate_seeds.py",
    }
    cohort_path.write_text(json.dumps(cohort, indent=2, ensure_ascii=False), encoding="utf-8")

    return len(tls_ids), str(cohort_path)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Generate seed skill artifacts for all TLS in a SUMO network")
    parser.add_argument("--net", default="sumo_scenarios/chengdu/chengdu.net.xml", help="SUMO network file")
    parser.add_argument("--output-dir", default="artifacts", help="Output directory")
    args = parser.parse_args()

    count, cohort_path = generate_seeds(args.net, args.output_dir)
    print(f"Generated seed artifacts for {count} TLS intersections")
    print(f"Seed cohort: {cohort_path}")


if __name__ == "__main__":
    main()
