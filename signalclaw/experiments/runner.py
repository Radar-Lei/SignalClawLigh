#!/usr/bin/env python3
"""
Experiment runner for comparing traffic signal control methods.

支持的方法:
- FixedTime: 不调用任何 skill，使用 SUMO 默认定时方案
- MaxPressure: 使用 MaxPressureSkill
- SignalClaw-Seed: 从 seed cohort 加载 frozen skills，使用 OnlineController 执行
- SignalClaw-Evolved: 从 evolved cohort 加载 frozen skills，使用 OnlineController 执行
"""

import os
import sys
import json
import csv
import time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from signalclaw.core.state import (
    NetworkObservation, IntersectionObservation,
    CyclePlan, PhaseCommand, PhaseObservation,
)
from signalclaw.core.metrics import StepMetrics, SimulationMetrics
from signalclaw.core.constraints import NetworkConstraints, IntersectionConstraints
from signalclaw.skills.max_pressure import MaxPressureSkill
from signalclaw.skills.signalclaw_skill import SignalClawSkill
from signalclaw.skills.cohort import SkillCohort
from signalclaw.network.neighbor_graph import NeighborGraph
from signalclaw.execution.online_controller import OnlineController


# ======================================================================
# 辅助函数
# ======================================================================

def is_green_phase(state_str: str) -> bool:
    """检查 phase state 字符串是否表示绿灯相位（有 g/G 但没有 y）。"""
    has_green = any(c in 'gG' for c in state_str)
    has_yellow = any(c in 'y' for c in state_str)
    return has_green and not has_yellow


def find_yellow_phase_before(td: dict, green_phase_idx: int) -> Optional[int]:
    """查找给定绿灯相位之前的黄灯/全红相位索引。

    在 SUMO TLS program 中，通常模式是: green -> yellow -> all_red -> next_green。
    如果 green_phase_idx > 0，前一个就是 yellow。
    """
    all_phases = td['all_phases']
    if green_phase_idx > 0:
        prev_idx = green_phase_idx - 1
        prev_state = td['phase_states'].get(prev_idx, '')
        if any(c in 'y' for c in prev_state) or all(c in 'rR' for c in prev_state):
            return prev_idx
    return None


# ======================================================================
# ExperimentRunner
# ======================================================================

class ExperimentRunner:
    """Run traffic signal control experiments in SUMO."""

    def __init__(self, sumocfg_path: str, seed: int = 42,
                 decision_interval: float = 5.0,
                 step_length: float = 1.0,
                 sim_duration: float = 3600.0):
        self.sumocfg_path = sumocfg_path
        self.seed = seed
        self.decision_interval = decision_interval
        self.step_length = step_length
        self.sim_duration = sim_duration
        self.results: Dict[str, SimulationMetrics] = {}

    # ------------------------------------------------------------------
    # 通用仿真主循环（用于所有方法）
    # ------------------------------------------------------------------

    def _run_simulation(self, method_name: str, controller,
                        verbose: bool = True) -> SimulationMetrics:
        """运行单次仿真。

        controller 可以是:
        - None: FixedTime，不做任何控制
        - MaxPressureSkill 实例
        - SignalClawSkill 实例
        - OnlineController 实例
        """
        import traci
        import sumolib

        metrics = SimulationMetrics(method_name=method_name)

        # 启动 SUMO
        cmd = ["sumo", "-c", self.sumocfg_path,
               "--seed", str(self.seed),
               "--step-length", str(self.step_length),
               "--no-warnings", "--no-step-log",
               "--time-to-teleport", "-1"]

        traci.start(cmd)

        # 解析网络
        net_path = os.path.join(os.path.dirname(self.sumocfg_path), "chengdu.net.xml")
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

        tls_ids = list(tls_data.keys())
        if verbose:
            print(f"  [{method_name}] Found {len(tls_ids)} traffic lights")

        # 车辆跟踪
        vehicle_depart: Dict[str, float] = {}
        vehicle_arrived: List[float] = []

        # 初始化控制器
        if controller is not None and hasattr(controller, 'reset'):
            controller.reset()

        # 每个 TLS 的追踪状态（用于非 OnlineController 方法）
        tls_state: Dict[str, dict] = {}
        for tid in tls_ids:
            tls_state[tid] = {
                'last_green_phase': None,
                'cycle_count': 0,
                'green_durations': {},
            }

        # OnlineController 的特殊状态
        is_online = isinstance(controller, OnlineController)
        # 为 OnlineController 初始化追踪状态（检测新绿色相位的进入）
        if is_online:
            controller._runner_states: Dict[str, dict] = {}
            for tid in tls_ids:
                controller._runner_states[tid] = {
                    'last_green_phase': None,
                    'plan_phase_index': 0,
                }

        step = 0
        sim_time = 0.0

        # 主循环
        while sim_time < self.sim_duration:
            traci.simulationStep()
            step += 1
            sim_time = traci.simulation.getTime()

            # 车辆跟踪
            for veh_id in traci.simulation.getDepartedIDList():
                vehicle_depart[veh_id] = sim_time
            for veh_id in traci.simulation.getArrivedIDList():
                if veh_id in vehicle_depart:
                    vehicle_arrived.append(sim_time - vehicle_depart[veh_id])
                    del vehicle_depart[veh_id]

            # --- 控制交通灯 ---
            if controller is None:
                # FixedTime: 不做任何控制
                pass

            elif is_online:
                # OnlineController 路径（SignalClaw-Seed / SignalClaw-Evolved）
                self._control_with_online_controller(
                    traci, tls_ids, tls_data, sim_time, controller
                )

            else:
                # 传统 skill 路径（MaxPressure / SignalClaw）
                self._control_with_legacy_skill(
                    traci, tls_ids, tls_data, sim_time, controller, tls_state
                )

            # 收集指标（每 10 步）
            if step % 10 == 0:
                total_queue = 0.0
                total_wait = 0.0
                n_lanes = 0

                for tls_id in tls_ids:
                    td = tls_data[tls_id]
                    for phase_idx, in_edges, out_edges in td['green_info']:
                        for edge in in_edges:
                            try:
                                for li in range(traci.edge.getLaneNumber(edge)):
                                    lane = f"{edge}_{li}"
                                    total_queue += traci.lane.getLastStepHaltingNumber(lane)
                                    total_wait += traci.lane.getWaitingTime(lane)
                                    n_lanes += 1
                            except traci.exceptions.TraCIException:
                                pass

                arrived_this_step = len(traci.simulation.getArrivedIDList())

                metrics.add_step(StepMetrics(
                    tls_id="ALL", step=step, sim_time=sim_time,
                    phase_id=0, queue_total=total_queue,
                    waiting_time_avg=total_wait / max(n_lanes, 1),
                    throughput=arrived_this_step,
                    delay_total=total_wait,
                    stops=int(total_queue * 0.3),
                ))

            if verbose and step % 600 == 0:
                last_m = metrics.step_metrics.get("ALL", [None])
                last_m = last_m[-1] if last_m else None
                q = last_m.queue_total if last_m else 0
                print(f"  [{method_name}] Step {step}, time={sim_time:.0f}s, queue={q:.0f}")

        metrics.travel_times = vehicle_arrived
        traci.close()

        if verbose:
            summary = metrics.summary()
            print(f"  [{method_name}] Completed: {summary['completed_vehicles']} vehicles, "
                  f"avg_travel={summary['avg_travel_time']:.1f}s, "
                  f"avg_queue={summary['avg_queue']:.1f}, "
                  f"avg_wait={summary['avg_waiting_time']:.1f}s")

        return metrics

    # ------------------------------------------------------------------
    # OnlineController 控制
    # ------------------------------------------------------------------

    def _control_with_online_controller(self, traci, tls_ids: list,
                                         tls_data: dict, sim_time: float,
                                         controller: OnlineController) -> None:
        """使用 OnlineController 控制所有路口。

        核心策略：只在检测到进入新的绿色相位时才干预，且只使用
        setPhaseDuration() 控制绿色相位持续时间。SUMO 自身负责
        绿 -> 黄 -> 红 -> 下一绿的过渡。

        这与 _control_with_legacy_skill 的行为模式一致，区别在于
        从 cohort 加载 per-TLS 的 frozen skill 而非使用全局 skill 实例。
        """
        # 收集所有路口的观测（用于 neighbor graph）
        all_obs: Dict[str, IntersectionObservation] = {}

        for tls_id in tls_ids:
            td = tls_data[tls_id]
            current_phase = traci.trafficlight.getPhase(tls_id)
            state_str = td['phases'][current_phase].state
            is_green = is_green_phase(state_str)

            # 检测是否刚进入一个新的绿色相位
            online_state = controller._runner_states[tls_id]
            last_green = online_state['last_green_phase']

            if is_green and last_green != current_phase:
                # 进入新的绿色相位
                online_state['last_green_phase'] = current_phase

                # 构建观测
                obs = self._build_observation(traci, tls_id, tls_data, current_phase)
                all_obs[tls_id] = obs

                # 构建 NetworkObservation（包含已收集的邻居观测）
                neighbor_ids = controller.neighbor_graph.get_neighbor_tls_ids(tls_id)
                neighbors: Dict[str, IntersectionObservation] = {}
                for nid in neighbor_ids:
                    if nid not in all_obs:
                        all_obs[nid] = self._build_observation(traci, nid, tls_data)
                    neighbors[nid] = all_obs[nid]

                net_obs = NetworkObservation(
                    ego=obs, neighbors=neighbors, timestamp=sim_time,
                )

                # 获取当前 plan 和跟踪状态
                plan = controller.cycle_manager.get_plan(tls_id)
                plan_idx = online_state['plan_phase_index']
                need_new_plan = (plan is None
                                 or plan_idx >= len(plan.phase_order)
                                 or plan.phase_order[plan_idx] != current_phase)

                if need_new_plan:
                    # 周期边界：调用 cycle skill 生成新计划
                    cycle_skill = controller.cohort.get_cycle_skill(tls_id)
                    raw_plan = cycle_skill.plan(net_obs)
                    plan = controller.safety_layer.clip_cycle_plan(raw_plan, tls_id)
                    controller.cycle_manager.set_plan(tls_id, plan, sim_time)
                    controller.stats.cycle_plan_count += 1
                    plan_idx = 0
                    online_state['plan_phase_index'] = 0

                # 从 plan 中获取当前绿色相位对应的持续时间
                if plan and plan.phase_order and plan_idx < len(plan.phase_order):
                    planned_phase = plan.phase_order[plan_idx]
                    duration = plan.green_times.get(
                        planned_phase, td['default_durations'][current_phase]
                    )
                else:
                    duration = td['default_durations'][current_phase]

                # 推进 plan_phase_index，下次进入新绿色相位时使用
                online_state['plan_phase_index'] = plan_idx + 1

                # 裁剪持续时间
                duration = max(8.0, min(90.0, duration))

                # 只使用 setPhaseDuration 控制当前绿色相位的持续时间
                traci.trafficlight.setPhaseDuration(tls_id, duration)
                controller.stats.phase_command_count += 1

    # ------------------------------------------------------------------
    # 传统 skill 控制（MaxPressure / SignalClaw）
    # ------------------------------------------------------------------

    def _control_with_legacy_skill(self, traci, tls_ids: list,
                                    tls_data: dict, sim_time: float,
                                    skill, tls_state: dict) -> None:
        """使用传统 skill 控制所有路口。"""
        for tls_id in tls_ids:
            td = tls_data[tls_id]
            state = tls_state[tls_id]
            current_phase = traci.trafficlight.getPhase(tls_id)

            is_green = is_green_phase(td['phases'][current_phase].state)

            if is_green and state['last_green_phase'] != current_phase:
                state['last_green_phase'] = current_phase

                obs = self._build_observation(traci, tls_id, tls_data, current_phase)

                neighbors = {}
                net_obs = NetworkObservation(
                    ego=obs, neighbors=neighbors, timestamp=sim_time,
                )

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
                            planned_phase, td['default_durations'][current_phase]
                        )
                    else:
                        duration = td['default_durations'][current_phase]

                    duration = max(8.0, min(90.0, duration))
                    traci.trafficlight.setPhaseDuration(tls_id, duration)
                    state['green_durations'][current_phase] = duration

    # ------------------------------------------------------------------
    # 观测构建
    # ------------------------------------------------------------------

    def _build_observation(self, traci, tls_id: str, tls_data: dict,
                           current_phase: int = None) -> IntersectionObservation:
        """从当前 SUMO 状态构建路口观测。"""
        td = tls_data[tls_id]

        if current_phase is None:
            current_phase = traci.trafficlight.getPhase(tls_id)

        # 计算当前相位已持续时间
        remaining = traci.trafficlight.getNextSwitch(tls_id) - traci.simulation.getTime()
        phase_duration = (td['default_durations'][current_phase]
                          if current_phase < len(td['default_durations']) else 30.0)
        elapsed = max(0.0, phase_duration - remaining)

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
                predicted_arrival=0.0,
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

    # ------------------------------------------------------------------
    # 构建约束
    # ------------------------------------------------------------------

    def _build_default_constraints(self, tls_ids: List[str]) -> NetworkConstraints:
        """为所有路口构建默认约束。"""
        intersections = {}
        for tid in tls_ids:
            intersections[tid] = IntersectionConstraints()
        return NetworkConstraints(intersections=intersections)

    # ------------------------------------------------------------------
    # 运行所有方法
    # ------------------------------------------------------------------

    def run_all(self, methods: Dict[str, Any] = None,
                verbose: bool = True):
        """运行所有方法。"""
        if methods is None:
            methods = {
                "FixedTime": None,
                "MaxPressure": MaxPressureSkill(decision_interval=self.decision_interval),
                "SignalClaw": SignalClawSkill(decision_interval=self.decision_interval),
            }

        for name, controller in methods.items():
            if verbose:
                print(f"\n{'=' * 60}")
                print(f"Running method: {name}")
                print(f"{'=' * 60}")
            self.results[name] = self._run_simulation(name, controller, verbose=verbose)

        return self.results

    def run_signalclaw_cohort(self, cohort_path: str, neighbor_graph_path: str,
                              method_name: str = "SignalClaw-Seed",
                              verbose: bool = True) -> SimulationMetrics:
        """使用 cohort 文件运行 SignalClaw 方法。"""
        # 先探测 tls_ids（启动一次 SUMO）
        import traci
        import sumolib

        cmd = ["sumo", "-c", self.sumocfg_path,
               "--seed", str(self.seed),
               "--step-length", str(self.step_length),
               "--no-warnings", "--no-step-log"]
        traci.start(cmd)
        net_path = os.path.join(os.path.dirname(self.sumocfg_path), "chengdu.net.xml")
        net = sumolib.net.readNet(net_path, withPrograms=True)
        tls_ids = [tls.getID() for tls in net.getTrafficLights()]
        traci.close()

        # 加载 cohort
        cohort = SkillCohort.load(cohort_path)

        # 加载 neighbor graph
        if os.path.exists(neighbor_graph_path):
            neighbor_graph = NeighborGraph.load(neighbor_graph_path)
        else:
            neighbor_graph = NeighborGraph()

        # 构建约束
        constraints = self._build_default_constraints(tls_ids)

        # 创建 OnlineController
        controller = OnlineController(
            cohort=cohort,
            neighbor_graph=neighbor_graph,
            constraints=constraints,
            decision_interval=self.decision_interval,
            sim_step_length=self.step_length,
        )

        if verbose:
            print(f"\n{'=' * 60}")
            print(f"Running method: {method_name}")
            print(f"  Cohort: {cohort.cohort_id}")
            print(f"  Crossings: {len(cohort.skills)}")
            print(f"{'=' * 60}")

        result = self._run_simulation(method_name, controller, verbose=verbose)
        self.results[method_name] = result
        return result

    # ------------------------------------------------------------------
    # 结果输出
    # ------------------------------------------------------------------

    def print_comparison(self):
        """打印比较结果。"""
        method_names = list(self.results.keys())

        header = f"{'Metric':<30}"
        for name in method_names:
            header += f" {name:>15}"
        width = 30 + 15 * len(method_names)

        print(f"\n{'=' * width}")
        print(f"{'EXPERIMENT RESULTS':^{width}}")
        print(f"{'=' * width}")
        print(header)
        print(f"{'-' * width}")

        summaries = {name: m.summary() for name, m in self.results.items()}

        metrics_list = [
            ("Avg Travel Time (s)", "avg_travel_time", False),
            ("Completed Vehicles", "completed_vehicles", True),
            ("Avg Queue Length", "avg_queue", False),
            ("Max Queue Length", "max_queue", False),
            ("Avg Waiting Time (s)", "avg_waiting_time", False),
            ("Max Waiting Time (s)", "max_waiting_time", False),
            ("Total Stops", "total_stops", False),
        ]

        for label, key, higher_better in metrics_list:
            vals = []
            for name in method_names:
                v = summaries.get(name, {}).get(key, 0)
                vals.append(v)

            best_idx = vals.index(max(vals) if higher_better else min(vals))

            line = f"{label:<30}"
            for i, v in enumerate(vals):
                marker = " *" if i == best_idx else "  "
                if isinstance(v, float):
                    line += f"{v:>12.1f}{marker}"
                else:
                    line += f"{v:>12}{marker}"
            print(line)

        print(f"{'-' * width}")
        print(f"* = best value")

        # SignalClaw 方法与第一个 baseline 的对比
        sc_names = [n for n in method_names if "SignalClaw" in n]
        baseline_name = method_names[0] if method_names else None

        if sc_names and baseline_name and baseline_name in summaries:
            for sc_name in sc_names:
                sc = summaries.get(sc_name, {})
                bl = summaries.get(baseline_name, {})
                if sc and bl:
                    print(f"\n{f'{sc_name} vs {baseline_name}':^{width}}")
                    print(f"{'-' * width}")
                    for label, key, higher_better in metrics_list:
                        sc_v = sc.get(key, 0)
                        bl_v = bl.get(key, 0)
                        if bl_v != 0:
                            pct = (sc_v - bl_v) / abs(bl_v) * 100
                            if higher_better:
                                better = "BETTER" if pct > 0 else "worse"
                            else:
                                better = "BETTER" if pct < 0 else "worse"
                            print(f"  {label:<30}: {pct:+.1f}% ({better})")

    def save_results(self, output_dir: str = "results"):
        """保存结果到文件。"""
        os.makedirs(output_dir, exist_ok=True)

        summaries = {name: m.summary() for name, m in self.results.items()}
        with open(os.path.join(output_dir, "summary.json"), "w") as f:
            json.dump(summaries, f, indent=2)

        for name, m in self.results.items():
            if "ALL" in m.step_metrics:
                with open(os.path.join(output_dir, f"{name}_steps.csv"), "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["step", "sim_time", "queue_total", "waiting_time_avg",
                                     "throughput", "delay_total", "stops"])
                    for sm in m.step_metrics["ALL"]:
                        writer.writerow([sm.step, sm.sim_time, sm.queue_total,
                                         sm.waiting_time_avg, sm.throughput,
                                         sm.delay_total, sm.stops])

        print(f"Results saved to {output_dir}/")


# ======================================================================
# main
# ======================================================================

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(os.path.dirname(script_dir))
    sumocfg_path = os.path.join(project_dir, "sumo_scenarios", "chengdu", "chengdu.sumocfg")

    print(f"SUMO config: {sumocfg_path}")
    print(f"Simulation time: 3600s")

    runner = ExperimentRunner(sumocfg_path=sumocfg_path, seed=42,
                               decision_interval=5.0)

    # --- 运行传统方法 ---
    runner.run_all()

    # --- 运行 SignalClaw-Seed（如果 cohort 存在）---
    seed_cohort_path = os.path.join(project_dir, "artifacts", "skills", "cohorts", "seed_cohort.json")
    neighbor_graph_path = os.path.join(project_dir, "artifacts", "topology", "one_hop_neighbors.json")

    if os.path.exists(seed_cohort_path):
        try:
            runner.run_signalclaw_cohort(
                cohort_path=seed_cohort_path,
                neighbor_graph_path=neighbor_graph_path,
                method_name="SignalClaw-Seed",
            )
        except Exception as e:
            print(f"  [SignalClaw-Seed] Failed: {e}")

    # --- 运行 SignalClaw-Evolved（如果 cohort 存在）---
    evolved_cohort_path = os.path.join(project_dir, "artifacts", "skills", "cohorts", "evolved_cohort.json")
    if os.path.exists(evolved_cohort_path):
        try:
            runner.run_signalclaw_cohort(
                cohort_path=evolved_cohort_path,
                neighbor_graph_path=neighbor_graph_path,
                method_name="SignalClaw-Evolved",
            )
        except Exception as e:
            print(f"  [SignalClaw-Evolved] Failed: {e}")

    runner.print_comparison()
    runner.save_results(os.path.join(project_dir, "results"))


if __name__ == "__main__":
    main()
