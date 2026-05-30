#!/usr/bin/env python3
"""
逐路口、逐时刻对比报告生成工具（Parity Report）

比较 Legacy SignalClaw 与 Seed artifact 在每个路口、每个采样时刻的性能差异，
输出 parity_report.csv 和 parity_summary.txt。

用法:
    # 直接运行两场仿真（需要 SUMO 环境）
    python -m signalclaw.scripts.parity_report --sumocfg <path> --cohort <path>

    # 从已有的 step-level CSV 文件生成对比（无需 SUMO）
    python -m signalclaw.scripts.parity_report \
        --legacy-csv results/SignalClaw_steps.csv \
        --seed-csv results/SignalClaw-Seed_steps.csv \
        --output-dir results_parity

    # 指定采样间隔（仿真模式，默认 10 步）
    python -m signalclaw.scripts.parity_report \
        --sumocfg <path> --cohort <path> --sample-interval 5
"""

import os
import sys
import csv
import json
import argparse
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from signalclaw.core.state import (
    NetworkObservation, IntersectionObservation,
    PhaseCommand,
)
from signalclaw.core.metrics import StepMetrics, SimulationMetrics
from signalclaw.core.constraints import NetworkConstraints, IntersectionConstraints
from signalclaw.skills.signalclaw_skill import SignalClawSkill
from signalclaw.skills.cohort import SkillCohort
from signalclaw.network.neighbor_graph import NeighborGraph
from signalclaw.execution.online_controller import OnlineController
from signalclaw.execution.phase_command_executor import PhaseCommandExecutor, is_green_phase


# ======================================================================
# 逐路口数据收集器
# ======================================================================

class PerTLSDataCollector:
    """在仿真过程中逐步收集每个路口的 queue、waiting、throughput。

    与 runner.py 的 TripInfoCollector 不同，这个收集器关注的是逐路口级别的数据，
    而不是全网汇总。每 sample_interval 步采样一次，记录每个 TLS 的快照。
    """

    def __init__(self, sample_interval: int = 10):
        self.sample_interval = sample_interval
        # {tls_id: [(sim_time, queue, waiting, throughput, current_phase), ...]}
        self.records: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._prev_arrived_per_tls: Dict[str, int] = {}
        # 全网累计完成车辆数（用于 summary）
        self._total_completed: int = 0
        self._vehicle_depart: Dict[str, float] = {}

    def maybe_sample(self, traci, step: int, sim_time: float,
                     tls_ids: List[str], tls_data: dict) -> bool:
        """每隔 sample_interval 步采样一次，返回是否采样了。"""
        if step % self.sample_interval != 0:
            return False

        current_arrived = traci.simulation.getArrivedNumber()

        for tls_id in tls_ids:
            td = tls_data[tls_id]
            current_phase = traci.trafficlight.getPhase(tls_id)

            tls_queue = 0.0
            tls_waiting = 0.0
            n_lanes = 0

            for _phase_idx, in_edges, _out_edges in td['green_info']:
                for edge in in_edges:
                    try:
                        n_lanes_edge = traci.edge.getLaneNumber(edge)
                        for li in range(n_lanes_edge):
                            lane = f"{edge}_{li}"
                            tls_queue += traci.lane.getLastStepHaltingNumber(lane)
                            tls_waiting += traci.lane.getWaitingTime(lane)
                            n_lanes += 1
                    except traci.exceptions.TraCIException:
                        pass

            # 逐路口 throughput: outgoing edges 上的移动车辆数
            tls_throughput = 0
            all_outgoing: set = set()
            for _phase_idx, _in_edges, out_edges in td['green_info']:
                all_outgoing.update(out_edges)
            for edge_id in all_outgoing:
                try:
                    n_veh = traci.edge.getLastStepVehicleNumber(edge_id)
                    n_halting = 0
                    for li in range(traci.edge.getLaneNumber(edge_id)):
                        n_halting += traci.lane.getLastStepHaltingNumber(f"{edge_id}_{li}")
                    tls_throughput += max(n_veh - n_halting, 0)
                except traci.exceptions.TraCIException:
                    pass

            self.records[tls_id].append({
                'sim_time': sim_time,
                'tls_id': tls_id,
                'current_phase': current_phase,
                'queue': tls_queue,
                'waiting': tls_waiting / max(n_lanes, 1),
                'throughput': tls_throughput,
            })

        return True

    def collect_departed(self, traci, sim_time: float):
        """记录新出发的车辆。"""
        for veh_id in traci.simulation.getDepartedIDList():
            self._vehicle_depart[veh_id] = sim_time

    def collect_arrived(self, traci, sim_time: float):
        """记录到达的车辆。"""
        for veh_id in traci.simulation.getArrivedIDList():
            if veh_id in self._vehicle_depart:
                self._total_completed += 1
                del self._vehicle_depart[veh_id]

    def get_total_completed(self) -> int:
        return self._total_completed


# ======================================================================
# 仿真运行（逐路口数据收集）
# ======================================================================

def _parse_tls_data(traci, net_path: str) -> Tuple[List[str], dict]:
    """解析网络 TLS 数据，返回 (tls_ids, tls_data)。"""
    import sumolib
    net = sumolib.net.readNet(net_path, withPrograms=True, withConnections=True)
    tls_data = {}
    for tls in net.getTrafficLights():
        tls_id = tls.getID()
        programs = tls.getPrograms()
        if not programs:
            continue
        prog = list(programs.values())[0]
        phases = prog.getPhases()
        connections = tls.getConnections()

        green_info = []
        phase_incoming = {}
        phase_outgoing = {}

        for i, phase in enumerate(phases):
            state = phase.state
            if is_green_phase(state):
                in_edges = set()
                out_edges = set()
                for conn in connections:
                    link_idx = conn[2] if len(conn) > 2 else 0
                    if link_idx < len(state) and state[link_idx] in 'gG':
                        in_edges.add(conn[0].getEdge().getID())
                        out_edges.add(conn[1].getEdge().getID())
                green_info.append((i, list(in_edges), list(out_edges)))
                phase_incoming[i] = list(in_edges)
                phase_outgoing[i] = list(out_edges)

        tls_data[tls_id] = {
            'phases': phases,
            'green_info': green_info,
            'green_indices': [gi[0] for gi in green_info],
            'num_phases': len(phases),
            'default_durations': [ph.duration for ph in phases],
            'phase_states': {i: phases[i].state for i in range(len(phases))},
            'phase_incoming': phase_incoming,
            'phase_outgoing': phase_outgoing,
            'all_phases': list(range(len(phases))),
        }

    return list(tls_data.keys()), tls_data


def run_simulation_with_per_tls(
    sumocfg_path: str,
    method_name: str,
    controller,
    seed: int = 42,
    step_length: float = 1.0,
    sim_duration: float = 3600.0,
    sample_interval: int = 10,
    verbose: bool = True,
) -> Tuple[Dict[str, List[Dict]], int]:
    """运行单次仿真，收集逐路口数据。

    返回:
        (per_tls_records, completed_vehicles)
        per_tls_records: {tls_id: [{sim_time, tls_id, current_phase, queue, waiting, throughput}, ...]}
    """
    import traci

    collector = PerTLSDataCollector(sample_interval=sample_interval)

    cmd = ["sumo", "-c", sumocfg_path,
           "--seed", str(seed),
           "--step-length", str(step_length),
           "--no-warnings", "--no-step-log",
           "--time-to-teleport", "-1"]
    traci.start(cmd)

    net_path = os.path.join(os.path.dirname(sumocfg_path), "chengdu.net.xml")
    tls_ids, tls_data = _parse_tls_data(traci, net_path)

    if verbose:
        print(f"  [{method_name}] Found {len(tls_ids)} traffic lights")

    # 初始化控制器
    if controller is not None and hasattr(controller, 'reset'):
        controller.reset()

    tls_state: Dict[str, dict] = {}
    for tid in tls_ids:
        tls_state[tid] = {
            'last_green_phase': None,
            'cycle_count': 0,
            'green_durations': {},
        }

    is_online = isinstance(controller, OnlineController)
    phase_executor = None
    if is_online:
        constraints = controller.safety_layer.constraints
        phase_executor = PhaseCommandExecutor.for_traci(traci, tls_data, constraints)

    step = 0
    sim_time = 0.0

    while sim_time < sim_duration:
        traci.simulationStep()
        step += 1
        sim_time = traci.simulation.getTime()

        # 收集车辆数据
        collector.collect_departed(traci, sim_time)
        collector.collect_arrived(traci, sim_time)

        # 采样逐路口快照
        collector.maybe_sample(traci, step, sim_time, tls_ids, tls_data)

        # 控制交通灯
        if controller is None:
            pass
        elif is_online:
            _control_online(traci, tls_ids, tls_data, sim_time,
                            controller, phase_executor)
        else:
            _control_legacy(traci, tls_ids, tls_data, sim_time,
                            controller, tls_state)

        if verbose and step % 600 == 0:
            print(f"  [{method_name}] Step {step}, time={sim_time:.0f}s")

    traci.close()

    if verbose:
        print(f"  [{method_name}] Completed: {collector.get_total_completed()} vehicles, "
              f"{len(collector.records)} TLS tracked")

    return dict(collector.records), collector.get_total_completed()


def _control_online(traci, tls_ids, tls_data, sim_time, controller, executor):
    """OnlineController 控制路径（复用 runner.py 逻辑）。"""
    all_obs: Dict[str, IntersectionObservation] = {}
    for tls_id in tls_ids:
        current_phase = traci.trafficlight.getPhase(tls_id)
        all_obs[tls_id] = _build_observation(traci, tls_id, tls_data, current_phase)

    executor.process_pending_switches(tls_ids)

    for tls_id in tls_ids:
        cmd = controller.step(tls_id, sim_time, all_obs)
        if cmd is not None:
            executor.apply(cmd, tls_id)


def _control_legacy(traci, tls_ids, tls_data, sim_time, skill, tls_state):
    """传统 skill 控制路径（复用 runner.py 逻辑）。"""
    for tls_id in tls_ids:
        td = tls_data[tls_id]
        state = tls_state[tls_id]
        current_phase = traci.trafficlight.getPhase(tls_id)

        if is_green_phase(td['phases'][current_phase].state):
            if state['last_green_phase'] != current_phase:
                state['last_green_phase'] = current_phase

                obs = _build_observation(traci, tls_id, tls_data, current_phase)
                net_obs = NetworkObservation(ego=obs, neighbors={}, timestamp=sim_time)
                plan = skill.plan_cycle(net_obs)

                if plan is not None:
                    green_idx_in_plan = None
                    for i, gi in enumerate(td['green_info']):
                        if gi[0] == current_phase:
                            green_idx_in_plan = i
                            break

                    if (green_idx_in_plan is not None
                            and green_idx_in_plan < len(plan.phase_order)):
                        planned_phase = plan.phase_order[green_idx_in_plan]
                        duration = plan.green_times.get(
                            planned_phase, td['default_durations'][current_phase])
                    else:
                        duration = td['default_durations'][current_phase]

                    duration = max(8.0, min(90.0, duration))
                    traci.trafficlight.setPhaseDuration(tls_id, duration)
                    state['green_durations'][current_phase] = duration


# 观测构建常量（与 runner.py 保持一致）
_DEFAULT_PREDICTED_ARRIVAL = 0.0


def _build_observation(traci, tls_id, tls_data, current_phase=None):
    """构建路口观测（复用 runner.py 的 _build_observation 逻辑）。"""
    td = tls_data[tls_id]
    if current_phase is None:
        current_phase = traci.trafficlight.getPhase(tls_id)

    remaining = traci.trafficlight.getNextSwitch(tls_id) - traci.simulation.getTime()
    phase_duration = (td['default_durations'][current_phase]
                      if current_phase < len(td['default_durations']) else 30.0)
    elapsed = max(0.0, phase_duration - remaining)

    from signalclaw.core.state import PhaseObservation

    phases_obs = {}
    all_downstream_q: Dict[str, float] = {}
    all_upstream_q: Dict[str, float] = {}

    for phase_idx, in_edges, out_edges in td['green_info']:
        queue = 0.0
        wait = 0.0
        n = 0
        for edge in in_edges:
            try:
                for li in range(traci.edge.getLaneNumber(edge)):
                    lane = f"{edge}_{li}"
                    queue += traci.lane.getLastStepHaltingNumber(lane)
                    wait += traci.lane.getWaitingTime(lane)
                    n += 1
            except traci.exceptions.TraCIException:
                pass

        down_q: Dict[str, float] = {}
        for edge in out_edges:
            try:
                q = sum(traci.lane.getLastStepHaltingNumber(f"{edge}_{li}")
                        for li in range(traci.edge.getLaneNumber(edge)))
                down_q[edge] = q
                all_downstream_q[edge] = q
            except traci.exceptions.TraCIException:
                pass

        for edge in in_edges:
            try:
                q = sum(traci.lane.getLastStepHaltingNumber(f"{edge}_{li}")
                        for li in range(traci.edge.getLaneNumber(edge)))
                all_upstream_q[edge] = q
            except traci.exceptions.TraCIException:
                pass

        phases_obs[phase_idx] = PhaseObservation(
            phase_id=phase_idx,
            queue=queue,
            waiting_time=wait / max(n, 1),
            predicted_arrival=_DEFAULT_PREDICTED_ARRIVAL,
            elapsed_green=elapsed if phase_idx == current_phase else 0.0,
            min_green=10.0,
            max_green=60.0,
        )

    return IntersectionObservation(
        crossing_id=tls_id,
        current_phase_id=current_phase,
        current_phase_elapsed=elapsed,
        cycle_second=0.0,
        phases=phases_obs,
        downstream_queue=all_downstream_q,
        upstream_queue=all_upstream_q,
    )


# ======================================================================
# 从已有 CSV 加载数据（无需 SUMO）
# ======================================================================

def load_step_csv(csv_path: str) -> List[Dict[str, Any]]:
    """加载 runner.py 输出的 step-level CSV 文件。

    现有 CSV 格式:
    step, sim_time, queue_total, waiting_time_avg, throughput, delay_total, stops
    （全网汇总，无 tls_id）

    返回格式与 PerTLSDataCollector.records["ALL"] 兼容。
    """
    records = []
    with open(csv_path, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append({
                'sim_time': float(row['sim_time']),
                'tls_id': 'ALL',
                'current_phase': -1,
                'queue': float(row['queue_total']),
                'waiting': float(row['waiting_time_avg']),
                'throughput': float(row['throughput']),
            })
    return records


# ======================================================================
# Parity Report 生成
# ======================================================================

def generate_parity_report(
    legacy_records: Dict[str, List[Dict]],
    seed_records: Dict[str, List[Dict]],
    legacy_completed: int,
    seed_completed: int,
    output_dir: str,
    tolerance_pct: float = 3.0,
) -> str:
    """生成 parity_report.csv 和 parity_summary.txt。

    参数:
        legacy_records: {tls_id: [{sim_time, queue, waiting, throughput, current_phase}, ...]}
        seed_records: 同上
        legacy_completed: legacy 完成车辆数
        seed_completed: seed 完成车辆数
        output_dir: 输出目录
        tolerance_pct: 验收容差百分比（默认 3%）

    返回:
        summary 文件路径
    """
    os.makedirs(output_dir, exist_ok=True)

    csv_path = os.path.join(output_dir, "parity_report.csv")
    summary_path = os.path.join(output_dir, "parity_summary.txt")

    # --- 收集所有 tls_id（取交集）---
    legacy_tls_ids = set(legacy_records.keys())
    seed_tls_ids = set(seed_records.keys())
    common_tls_ids = sorted(legacy_tls_ids & seed_tls_ids)

    # 全网级别的 key
    has_all_key = 'ALL' in common_tls_ids

    # --- 按 (sim_time, tls_id) 建立索引 ---
    def build_index(records: Dict[str, List[Dict]]) -> Dict[Tuple[float, str], Dict]:
        idx = {}
        for tls_id, snapshots in records.items():
            for snap in snapshots:
                key = (round(snap['sim_time'], 1), tls_id)
                idx[key] = snap
        return idx

    legacy_idx = build_index(legacy_records)
    seed_idx = build_index(seed_records)

    # --- 收集所有 (sim_time, tls_id) 对 ---
    all_keys = sorted(set(legacy_idx.keys()) | set(seed_idx.keys()))

    # --- 写 CSV ---
    csv_rows = []
    for (sim_time, tls_id) in all_keys:
        legacy_snap = legacy_idx.get((sim_time, tls_id))
        seed_snap = seed_idx.get((sim_time, tls_id))

        legacy_queue = legacy_snap['queue'] if legacy_snap else None
        seed_queue = seed_snap['queue'] if seed_snap else None
        legacy_waiting = legacy_snap['waiting'] if legacy_snap else None
        seed_waiting = seed_snap['waiting'] if seed_snap else None
        legacy_throughput = legacy_snap['throughput'] if legacy_snap else None
        seed_throughput = seed_snap['throughput'] if seed_snap else None
        current_phase = legacy_snap.get('current_phase', seed_snap.get('current_phase', -1)) if (legacy_snap or seed_snap) else -1

        # 计算百分比差异（以 legacy 为基准）
        queue_diff_pct = _pct_diff(legacy_queue, seed_queue)
        waiting_diff_pct = _pct_diff(legacy_waiting, seed_waiting)

        csv_rows.append({
            'sim_time': sim_time,
            'tls_id': tls_id,
            'current_phase': current_phase,
            'legacy_queue': _fmt(legacy_queue),
            'seed_queue': _fmt(seed_queue),
            'queue_diff_pct': _fmt(queue_diff_pct),
            'legacy_waiting': _fmt(legacy_waiting),
            'seed_waiting': _fmt(seed_waiting),
            'waiting_diff_pct': _fmt(waiting_diff_pct),
            'legacy_throughput': _fmt(legacy_throughput),
            'seed_throughput': _fmt(seed_throughput),
        })

    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'sim_time', 'tls_id', 'current_phase',
            'legacy_queue', 'seed_queue', 'queue_diff_pct',
            'legacy_waiting', 'seed_waiting', 'waiting_diff_pct',
            'legacy_throughput', 'seed_throughput',
        ])
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"  Parity report CSV: {csv_path} ({len(csv_rows)} rows)")

    # --- 生成汇总报告 ---
    _generate_summary(
        csv_rows, legacy_completed, seed_completed,
        common_tls_ids, tolerance_pct, summary_path,
    )

    return summary_path


def _pct_diff(base, value) -> Optional[float]:
    """计算百分比差异 (value - base) / |base| * 100。"""
    if base is None or value is None or base == 0:
        return None
    return (value - base) / abs(base) * 100


def _fmt(val) -> str:
    """格式化数值，None -> 空字符串。"""
    if val is None:
        return ''
    if isinstance(val, float):
        return f'{val:.4f}'
    return str(val)


def _generate_summary(
    csv_rows: List[Dict],
    legacy_completed: int,
    seed_completed: int,
    tls_ids: List[str],
    tolerance_pct: float,
    summary_path: str,
):
    """生成 parity_summary.txt 汇总报告。"""
    lines = []
    lines.append("=" * 70)
    lines.append("PARITY REPORT SUMMARY")
    lines.append("Legacy SignalClaw vs SignalClaw-Seed (逐路口对比)")
    lines.append("=" * 70)
    lines.append("")

    # --- 1. 逐路口平均差异 ---
    lines.append("-" * 70)
    lines.append("1. 逐路口平均 Queue / Waiting 差异")
    lines.append("-" * 70)

    # 按 tls_id 分组
    tls_data: Dict[str, List[Dict]] = defaultdict(list)
    for row in csv_rows:
        tls_data[row['tls_id']].append(row)

    header = f"{'TLS ID':<20} {'Samples':>8} {'Avg Q(Legacy)':>14} {'Avg Q(Seed)':>14} {'Avg Q Diff%':>12} {'Avg W(Legacy)':>14} {'Avg W(Seed)':>14} {'Avg W Diff%':>12}"
    lines.append(header)
    lines.append("-" * len(header))

    for tls_id in sorted(tls_data.keys()):
        rows = tls_data[tls_id]
        n = len(rows)

        # 计算平均 queue 差异
        q_diffs = [float(r['queue_diff_pct']) for r in rows if r['queue_diff_pct']]
        avg_q_diff = sum(q_diffs) / len(q_diffs) if q_diffs else 0.0

        # 计算平均 waiting 差异
        w_diffs = [float(r['waiting_diff_pct']) for r in rows if r['waiting_diff_pct']]
        avg_w_diff = sum(w_diffs) / len(w_diffs) if w_diffs else 0.0

        # 平均 queue 值
        legacy_queues = [float(r['legacy_queue']) for r in rows if r['legacy_queue']]
        seed_queues = [float(r['seed_queue']) for r in rows if r['seed_queue']]
        avg_legacy_q = sum(legacy_queues) / len(legacy_queues) if legacy_queues else 0.0
        avg_seed_q = sum(seed_queues) / len(seed_queues) if seed_queues else 0.0

        # 平均 waiting 值
        legacy_waits = [float(r['legacy_waiting']) for r in rows if r['legacy_waiting']]
        seed_waits = [float(r['seed_waiting']) for r in rows if r['seed_waiting']]
        avg_legacy_w = sum(legacy_waits) / len(legacy_waits) if legacy_waits else 0.0
        avg_seed_w = sum(seed_waits) / len(seed_waits) if seed_waits else 0.0

        lines.append(
            f"{tls_id:<20} {n:>8} {avg_legacy_q:>14.2f} {avg_seed_q:>14.2f} "
            f"{avg_q_diff:>+11.1f}% {avg_legacy_w:>14.4f} {avg_seed_w:>14.4f} "
            f"{avg_w_diff:>+11.1f}%"
        )

    lines.append("")

    # --- 2. 全网汇总指标 ---
    lines.append("-" * 70)
    lines.append("2. 全网汇总指标对比")
    lines.append("-" * 70)

    lines.append(f"  {'Metric':<30} {'Legacy':>12} {'Seed':>12} {'Delta%':>10}")
    lines.append(f"  {'-' * 66}")

    # Completed vehicles
    if legacy_completed > 0:
        cv_delta = (seed_completed - legacy_completed) / legacy_completed * 100
        lines.append(f"  {'Completed Vehicles':<30} {legacy_completed:>12} {seed_completed:>12} {cv_delta:>+9.1f}%")
    else:
        lines.append(f"  {'Completed Vehicles':<30} {legacy_completed:>12} {seed_completed:>12} {'N/A':>10}")

    # 全网 avg queue / waiting
    all_rows = tls_data.get('ALL', [])
    if all_rows:
        legacy_qs = [float(r['legacy_queue']) for r in all_rows if r['legacy_queue']]
        seed_qs = [float(r['seed_queue']) for r in all_rows if r['seed_queue']]
        if legacy_qs and seed_qs:
            lq = sum(legacy_qs) / len(legacy_qs)
            sq = sum(seed_qs) / len(seed_qs)
            qd = (sq - lq) / lq * 100 if lq != 0 else 0
            lines.append(f"  {'Avg Queue (ALL)':<30} {lq:>12.2f} {sq:>12.2f} {qd:>+9.1f}%")

        legacy_ws = [float(r['legacy_waiting']) for r in all_rows if r['legacy_waiting']]
        seed_ws = [float(r['seed_waiting']) for r in all_rows if r['seed_waiting']]
        if legacy_ws and seed_ws:
            lw = sum(legacy_ws) / len(legacy_ws)
            sw = sum(seed_ws) / len(seed_ws)
            wd = (sw - lw) / lw * 100 if lw != 0 else 0
            lines.append(f"  {'Avg Waiting (ALL)':<30} {lw:>12.4f} {sw:>12.4f} {wd:>+9.1f}%")

    lines.append("")

    # --- 3. 验收标准检查 ---
    lines.append("-" * 70)
    lines.append("3. 验收标准检查")
    lines.append("-" * 70)
    lines.append(f"  标准: Seed completed_vehicles 不应比 Legacy 低超过 {tolerance_pct:.1f}%")
    lines.append("")

    if legacy_completed > 0:
        cv_delta = (seed_completed - legacy_completed) / legacy_completed * 100
        passed = cv_delta >= -tolerance_pct

        status = "PASS" if passed else "FAIL"
        lines.append(f"  Legacy completed_vehicles: {legacy_completed}")
        lines.append(f"  Seed   completed_vehicles: {seed_completed}")
        lines.append(f"  Delta: {cv_delta:+.2f}%")
        lines.append(f"  Result: [{status}]")

        if not passed:
            lines.append(f"")
            lines.append(f"  WARNING: Seed 性能低于 Legacy {abs(cv_delta):.2f}%，"
                         f"超过 {tolerance_pct:.1f}% 容差阈值。")
            lines.append(f"  建议检查: cohort 质量、安全约束配置、决策间隔等。")
    else:
        lines.append("  N/A (无法计算，legacy_completed=0)")

    lines.append("")
    lines.append("=" * 70)
    lines.append("END OF REPORT")
    lines.append("=" * 70)

    summary_text = "\n".join(lines)
    with open(summary_path, 'w') as f:
        f.write(summary_text)

    print(f"  Parity summary: {summary_path}")
    print(summary_text)


# ======================================================================
# 主入口
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="逐路口对比报告：Legacy SignalClaw vs SignalClaw-Seed",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 从已有 CSV 文件生成对比（不需要 SUMO）
  python -m signalclaw.scripts.parity_report \\
      --legacy-csv results/SignalClaw_steps.csv \\
      --seed-csv results/SignalClaw-Seed_steps.csv

  # 直接运行仿真生成逐路口数据
  python -m signalclaw.scripts.parity_report \\
      --sumocfg sumo_scenarios/chengdu/chengdu.sumocfg \\
      --cohort artifacts/skills/cohorts/seed_cohort.json
        """,
    )

    # 模式 1: 从 CSV 加载
    parser.add_argument("--legacy-csv", type=str, default=None,
                        help="Legacy 方法的 step-level CSV 文件路径")
    parser.add_argument("--seed-csv", type=str, default=None,
                        help="Seed 方法的 step-level CSV 文件路径")

    # 模式 2: 直接运行仿真
    parser.add_argument("--sumocfg", type=str, default=None,
                        help="SUMO .sumocfg 文件路径")
    parser.add_argument("--cohort", type=str, default=None,
                        help="Seed cohort JSON 文件路径")
    parser.add_argument("--neighbor-graph", type=str, default=None,
                        help="邻居图 JSON 文件路径")

    # 通用参数
    parser.add_argument("--output-dir", type=str, default=None,
                        help="输出目录（默认: results_parity/）")
    parser.add_argument("--seed", type=int, default=42,
                        help="随机种子（默认: 42）")
    parser.add_argument("--sim-duration", type=float, default=3600.0,
                        help="仿真时长（秒，默认: 3600）")
    parser.add_argument("--sample-interval", type=int, default=10,
                        help="采样间隔（步数，默认: 10）")
    parser.add_argument("--tolerance", type=float, default=3.0,
                        help="验收容差百分比（默认: 3.0）")
    parser.add_argument("--decision-interval", type=float, default=5.0,
                        help="决策间隔（秒，默认: 5.0）")

    args = parser.parse_args()

    # 确定 project_dir
    project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if args.output_dir is None:
        args.output_dir = os.path.join(project_dir, "results_parity")

    # ================================================================
    # 模式判断
    # ================================================================
    legacy_records: Dict[str, List[Dict]] = {}
    seed_records: Dict[str, List[Dict]] = {}
    legacy_completed = 0
    seed_completed = 0

    if args.legacy_csv and args.seed_csv:
        # --- 模式 1: 从 CSV 加载 ---
        print("模式: 从已有 CSV 文件加载")
        print(f"  Legacy CSV: {args.legacy_csv}")
        print(f"  Seed CSV: {args.seed_csv}")

        legacy_data = load_step_csv(args.legacy_csv)
        seed_data = load_step_csv(args.seed_csv)

        legacy_records = {'ALL': legacy_data}
        seed_records = {'ALL': seed_data}

        # 尝试从 summary.json 获取 completed_vehicles
        legacy_summary_path = os.path.join(os.path.dirname(args.legacy_csv), "summary.json")
        seed_summary_path = os.path.join(os.path.dirname(args.seed_csv), "summary.json")

        if os.path.exists(legacy_summary_path):
            with open(legacy_summary_path) as f:
                legacy_summary = json.load(f)
            # 查找包含 "SignalClaw" 的 key
            for key, val in legacy_summary.items():
                if 'SignalClaw' in key and 'Seed' not in key and 'Evolved' not in key:
                    legacy_completed = val.get('completed_vehicles', 0)
                    break
                elif 'Legacy' in key:
                    legacy_completed = val.get('completed_vehicles', 0)
                    break

        if os.path.exists(seed_summary_path):
            with open(seed_summary_path) as f:
                seed_summary = json.load(f)
            for key, val in seed_summary.items():
                if 'Seed' in key:
                    seed_completed = val.get('completed_vehicles', 0)
                    break

        # 也尝试从 legacy_vs_seed.json 获取
        comparison_path = os.path.join(os.path.dirname(args.legacy_csv), "..", "results_comparison", "legacy_vs_seed.json")
        if os.path.exists(comparison_path):
            with open(comparison_path) as f:
                comp = json.load(f)
            for key, val in comp.items():
                if 'Legacy' in key:
                    legacy_completed = val.get('completed_vehicles', legacy_completed)
                elif 'Seed' in key:
                    seed_completed = val.get('completed_vehicles', seed_completed)

        print(f"  Legacy completed_vehicles: {legacy_completed}")
        print(f"  Seed completed_vehicles: {seed_completed}")
        print()

    elif args.sumocfg and args.cohort:
        # --- 模式 2: 直接运行仿真 ---
        print("模式: 直接运行仿真（逐路口数据收集）")
        print(f"  SUMO config: {args.sumocfg}")
        print(f"  Cohort: {args.cohort}")
        print()

        if args.neighbor_graph is None:
            args.neighbor_graph = os.path.join(
                project_dir, "artifacts", "topology", "one_hop_neighbors.json")

        # --- 运行 Legacy SignalClaw ---
        print("=" * 70)
        print("PHASE 1: Running Legacy SignalClaw（逐路口收集）")
        print("=" * 70)

        legacy_skill = SignalClawSkill(decision_interval=args.decision_interval)
        legacy_records, legacy_completed = run_simulation_with_per_tls(
            sumocfg_path=args.sumocfg,
            method_name="SignalClaw-Legacy",
            controller=legacy_skill,
            seed=args.seed,
            sim_duration=args.sim_duration,
            sample_interval=args.sample_interval,
            verbose=True,
        )
        print()

        # --- 运行 SignalClaw-Seed ---
        print("=" * 70)
        print("PHASE 2: Running SignalClaw-Seed（逐路口收集）")
        print("=" * 70)

        cohort = SkillCohort.load(args.cohort)
        neighbor_graph = NeighborGraph()
        if os.path.exists(args.neighbor_graph):
            neighbor_graph = NeighborGraph.load(args.neighbor_graph)

        # 先探测 tls_ids
        import traci
        import sumolib
        cmd = ["sumo", "-c", args.sumocfg, "--seed", str(args.seed),
               "--step-length", "1.0", "--no-warnings", "--no-step-log"]
        traci.start(cmd)
        net_path = os.path.join(os.path.dirname(args.sumocfg), "chengdu.net.xml")
        net = sumolib.net.readNet(net_path, withPrograms=True)
        tls_ids = [tls.getID() for tls in net.getTrafficLights()]
        traci.close()

        constraints = NetworkConstraints(
            intersections={tid: IntersectionConstraints() for tid in tls_ids}
        )
        controller = OnlineController(
            cohort=cohort,
            neighbor_graph=neighbor_graph,
            constraints=constraints,
            decision_interval=args.decision_interval,
            sim_step_length=1.0,
        )

        seed_records, seed_completed = run_simulation_with_per_tls(
            sumocfg_path=args.sumocfg,
            method_name="SignalClaw-Seed",
            controller=controller,
            seed=args.seed,
            sim_duration=args.sim_duration,
            sample_interval=args.sample_interval,
            verbose=True,
        )
        print()

    else:
        parser.error(
            "需要指定 (--legacy-csv + --seed-csv) 或 (--sumocfg + --cohort)"
        )

    # ================================================================
    # 生成报告
    # ================================================================
    print("=" * 70)
    print("GENERATING PARITY REPORT")
    print("=" * 70)

    generate_parity_report(
        legacy_records=legacy_records,
        seed_records=seed_records,
        legacy_completed=legacy_completed,
        seed_completed=seed_completed,
        output_dir=args.output_dir,
        tolerance_pct=args.tolerance,
    )


if __name__ == "__main__":
    main()
