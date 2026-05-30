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
import tempfile
import xml.etree.ElementTree as ET
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
from signalclaw.execution.phase_command_executor import PhaseCommandExecutor


# ======================================================================
# 常量
# ======================================================================

# PhaseObservation 中 predicted_arrival 的默认值。
# TODO: 理想情况下应从预测模块获取真实值。当 enable_prediction=False 时，
#       predicted_arrival 不可用，skill 不应依赖此特征做决策。
DEFAULT_PREDICTED_ARRIVAL = 0.0


# ======================================================================
# TripInfoCollector — 仿真过程中实时收集真实指标
# ======================================================================

class TripInfoCollector:
    """在仿真运行过程中逐步收集真实指标，替代仿真后解析 tripinfo XML。

    收集内容:
    - completed_vehicles: 完成旅程的车辆数
    - total_travel_time: 累计行程时间（从 departed 到 arrived）
    - total_waiting_time: 累计等待时间（从 traci.vehicle.getWaitingTime 逐车累加）
    - total_stops: 累计停车次数（从每步每 lane halting number 累加，是真实数据）
    - total_halting_time_steps: 总 halting 车道步数（用于计算时间加权平均 queue）
    - total_lanes_monitored: 监控的车道步数（用于计算时间加权平均 queue）
    """

    def __init__(self):
        # 车辆跟踪: departed -> sim_time
        self._vehicle_depart: Dict[str, float] = {}
        # 已完成车辆的 travel time 列表
        self._travel_times: List[float] = []
        # 已完成车辆的 waiting time 列表（从 traci.vehicle 获取）
        self._waiting_times: List[float] = []
        # 停车次数: 每步每 lane halting number 的累加（真实数据）
        self._total_stops: int = 0
        # 时间加权平均 queue 的分子: 每步所有 lane halting number 之和
        self._halting_sum: float = 0.0
        # 时间加权平均 queue 的分母: 每步监控的 lane 数
        self._lane_steps: int = 0
        # 完成车辆数
        self._completed_vehicles: int = 0

    def collect_departed(self, traci, sim_time: float):
        """记录每步新出发的车辆。"""
        for veh_id in traci.simulation.getDepartedIDList():
            self._vehicle_depart[veh_id] = sim_time

    def collect_arrived(self, traci, sim_time: float):
        """记录每步到达的车辆，收集其 travel time 和 waiting time。"""
        for veh_id in traci.simulation.getArrivedIDList():
            if veh_id in self._vehicle_depart:
                travel_time = sim_time - self._vehicle_depart[veh_id]
                self._travel_times.append(travel_time)
                self._completed_vehicles += 1
                del self._vehicle_depart[veh_id]

            # 尝试获取车辆累计 waiting time（到达瞬间仍可获取）
            try:
                wt = traci.vehicle.getWaitingTime(veh_id)
                if wt is not None and wt > 0:
                    self._waiting_times.append(wt)
            except (traci.exceptions.TraCIException, Exception):
                pass

    def collect_lane_stats(self, traci, lane_list: List[str]):
        """每步收集所有监控 lane 的 halting number（用于 stops 和 avg_queue）。

        这是真实的 stops 来源: 每步每 lane 有 halting 车辆即计为一次停车。
        总 stops = 每步每 lane halting number 的累加。
        avg_queue = halting_sum / lane_steps（时间加权平均）。
        """
        step_halting = 0
        step_lanes = 0
        for lane in lane_list:
            try:
                h = traci.lane.getLastStepHaltingNumber(lane)
                step_halting += h
                step_lanes += 1
            except traci.exceptions.TraCIException:
                pass
        self._total_stops += step_halting
        self._halting_sum += step_halting
        self._lane_steps += step_lanes

    def get_completed_vehicles(self) -> int:
        return self._completed_vehicles

    def get_travel_times(self) -> List[float]:
        return self._travel_times

    def get_waiting_times(self) -> List[float]:
        return self._waiting_times

    def get_total_stops(self) -> int:
        """真实停车次数: 每步每 lane halting number 累加。"""
        return self._total_stops

    def get_avg_queue(self) -> float:
        """时间加权平均 queue: 总 halting / 总 lane 步数。"""
        return self._halting_sum / max(self._lane_steps, 1)

    def get_report(self, simulated_hours: float) -> dict:
        """生成最终指标报告。"""
        cv = max(self._completed_vehicles, 1)
        return {
            'completed_vehicles': self._completed_vehicles,
            'throughput_per_hour': self._completed_vehicles / max(simulated_hours, 0.01),
            'avg_travel_time': sum(self._travel_times) / cv,
            'avg_waiting_time': sum(self._waiting_times) / max(len(self._waiting_times), 1),
            'total_stops': self._total_stops,
            'avg_queue': self.get_avg_queue(),
        }


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
                 sim_duration: float = 3600.0,
                 enable_prediction: bool = False):
        self.sumocfg_path = sumocfg_path
        self.seed = seed
        self.decision_interval = decision_interval
        self.step_length = step_length
        self.sim_duration = sim_duration
        # 是否启用到达车辆预测。为 False 时 PhaseObservation.predicted_arrival
        # 使用 DEFAULT_PREDICTED_ARRIVAL（0.0），并在观测中标注不可用。
        # TODO: 接入真实预测模块后默认改为 True。
        self.enable_prediction = enable_prediction
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

        # 创建 tripinfo 临时文件
        tripinfo_file = tempfile.NamedTemporaryFile(
            suffix=".xml", prefix="tripinfo_", delete=False
        )
        tripinfo_path = tripinfo_file.name
        tripinfo_file.close()

        # 启动 SUMO（启用 tripinfo 输出以获取真实 travel time / waiting time / stops）
        cmd = ["sumo", "-c", self.sumocfg_path,
               "--seed", str(self.seed),
               "--step-length", str(self.step_length),
               "--no-warnings", "--no-step-log",
               "--time-to-teleport", "-1",
               "--tripinfo-output", tripinfo_path]

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

        # ------------------------------------------------------------------
        # 预构建所有监控 lane 列表（避免每步重复字符串拼接和 edge 查询）
        # ------------------------------------------------------------------
        monitored_lanes: List[str] = []
        for tls_id in tls_ids:
            td = tls_data[tls_id]
            for _phase_idx, in_edges, _out_edges in td['green_info']:
                for edge in in_edges:
                    try:
                        n_lanes_edge = traci.edge.getLaneNumber(edge)
                        for li in range(n_lanes_edge):
                            monitored_lanes.append(f"{edge}_{li}")
                    except traci.exceptions.TraCIException:
                        pass
        # 去重（同一 lane 可能被多个 phase 引用）
        monitored_lanes = list(dict.fromkeys(monitored_lanes))

        # ------------------------------------------------------------------
        # 初始化 TripInfoCollector（逐步收集真实指标）
        # ------------------------------------------------------------------
        collector = TripInfoCollector()

        # 累计到达车辆数（用于计算每步 throughput 差值）
        last_arrived_count = 0

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

        # 为 OnlineController 创建 PhaseCommandExecutor（跨 step 保持 pending 状态）
        phase_executor = None
        if is_online:
            phase_executor = PhaseCommandExecutor.for_traci(
                traci, tls_data, controller.safety_layer.constraints
            )

        step = 0
        sim_time = 0.0

        # 主循环
        while sim_time < self.sim_duration:
            traci.simulationStep()
            step += 1
            sim_time = traci.simulation.getTime()

            # --- 逐步收集车辆指标（真实数据）---
            collector.collect_departed(traci, sim_time)
            collector.collect_arrived(traci, sim_time)

            # --- 逐步收集 lane 级 halting 数据（真实 stops + avg_queue）---
            collector.collect_lane_stats(traci, monitored_lanes)

            # --- 控制交通灯 ---
            if controller is None:
                # FixedTime: 不做任何控制
                pass

            elif is_online:
                # OnlineController 路径（SignalClaw-Seed / SignalClaw-Evolved）
                self._control_with_online_controller(
                    traci, tls_ids, tls_data, sim_time, controller, phase_executor
                )

            else:
                # 传统 skill 路径（MaxPressure / SignalClaw）
                self._control_with_legacy_skill(
                    traci, tls_ids, tls_data, sim_time, controller, tls_state
                )

            # 收集指标快照（每 10 步，用于时间序列分析和可视化）
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

                # 使用累计到达数差值计算区间 throughput（统一口径）
                current_arrived_count = traci.simulation.getArrivedNumber()
                throughput_delta = current_arrived_count - last_arrived_count
                last_arrived_count = current_arrived_count

                # 真实 stops: 使用 TripInfoCollector 的累计值，再减去上次的快照值
                metrics.add_step(StepMetrics(
                    tls_id="ALL", step=step, sim_time=sim_time,
                    phase_id=0, queue_total=total_queue,
                    waiting_time_avg=total_wait / max(n_lanes, 1),
                    throughput=throughput_delta,
                    delay_total=total_wait,
                    # 真实 stops: 从 TripInfoCollector 获取累计 halting number
                    stops=collector.get_total_stops(),
                ))

            if verbose and step % 600 == 0:
                last_m = metrics.step_metrics.get("ALL", [None])
                last_m = last_m[-1] if last_m else None
                q = last_m.queue_total if last_m else 0
                print(f"  [{method_name}] Step {step}, time={sim_time:.0f}s, queue={q:.0f}")

        # --- 使用 TripInfoCollector 的真实数据 ---
        metrics.travel_times = collector.get_travel_times()
        metrics.total_sim_time = sim_time

        # 使用 TripInfoCollector 的时间加权平均 queue（真实数据，非采样平均）
        metrics._collector_avg_queue = collector.get_avg_queue()

        # 真实 stops 来自 TripInfoCollector 的逐步累加
        metrics.total_stops_from_tripinfo = collector.get_total_stops()

        # --- 收集 OnlineController 统计日志 ---
        if is_online and controller is not None:
            ctrl_stats = controller.stats.to_dict()
            # 确保 online_glm_calls 始终为 0
            ctrl_stats["online_glm_calls"] = 0
            metrics.controller_stats = ctrl_stats
            if verbose:
                print(f"  [{method_name}] Controller stats: "
                      f"cycle_plan={ctrl_stats['cycle_plan_count']}, "
                      f"phase_cmd={ctrl_stats['phase_command_count']}, "
                      f"hold={ctrl_stats['phase_hold_count']}, "
                      f"switch={ctrl_stats['phase_switch_count']}, "
                      f"extend={ctrl_stats['phase_extend_count']}, "
                      f"shorten={ctrl_stats['phase_shorten_count']}, "
                      f"safety_clip={ctrl_stats['safety_clip_count']}")
                print(f"  [{method_name}] Switch constraints: "
                      f"cooldown_reject={ctrl_stats.get('switch_cooldown_reject_count', 0)}, "
                      f"min_green_reject={ctrl_stats.get('min_green_reject_count', 0)}, "
                      f"cycle_limit_reject={ctrl_stats.get('cycle_switch_limit_reject_count', 0)}")

        traci.close()

        # --- 交叉验证: 从 tripinfo XML 解析 travel time / waiting time ---
        tripinfo_travel_times: List[float] = []
        tripinfo_waiting_times: List[float] = []

        try:
            tree = ET.parse(tripinfo_path)
            root = tree.getroot()
            for trip in root.findall('tripinfo'):
                tt = trip.get('duration')
                if tt is not None:
                    tripinfo_travel_times.append(float(tt))
                wt = trip.get('waitingTime')
                if wt is not None:
                    tripinfo_waiting_times.append(float(wt))

            # tripinfo XML 优先级最高（仿真后的完整数据），覆盖实时收集的结果
            if tripinfo_travel_times:
                metrics.travel_times = tripinfo_travel_times
                metrics.waiting_times = tripinfo_waiting_times

            if verbose:
                src = "tripinfo" if tripinfo_travel_times else "collector_realtime"
                print(f"  [{method_name}] Metrics source: {src} "
                      f"({len(tripinfo_travel_times)} trips from XML, "
                      f"{collector.get_completed_vehicles()} from realtime)")
        except (ET.ParseError, FileNotFoundError) as e:
            if verbose:
                print(f"  [{method_name}] tripinfo parse failed ({e}), "
                      f"using realtime collector metrics")
        finally:
            # 清理临时文件
            try:
                os.unlink(tripinfo_path)
            except OSError:
                pass

        if verbose:
            summary = metrics.summary()
            stops_str = (str(summary['total_stops'])
                         if summary['total_stops'] is not None else "N/A")
            print(f"  [{method_name}] Completed: {summary['completed_vehicles']} vehicles, "
                  f"avg_travel={summary['avg_travel_time']:.1f}s, "
                  f"avg_queue={summary['avg_queue']:.1f}, "
                  f"avg_wait={summary['avg_waiting_time']:.1f}s "
                  f"(src={summary['waiting_time_source']}), "
                  f"stops={stops_str} (src={summary['stops_source']})")

        return metrics

    # ------------------------------------------------------------------
    # OnlineController 控制
    # ------------------------------------------------------------------

    def _control_with_online_controller(self, traci, tls_ids: list,
                                         tls_data: dict, sim_time: float,
                                         controller: OnlineController,
                                         executor: PhaseCommandExecutor) -> None:
        """使用 OnlineController 控制所有路口 — 完整双 Skill 闭环。

        每个 sim step 对每个 tls_id 调用 online_controller.step()，
        将返回的 PhaseCommand 映射为 TraCI 操作：
        - hold:    保持当前相位，设置剩余 duration
        - extend:  延长当前绿色相位 duration
        - shorten: 缩短当前绿色相位 duration（不低于 min_green）
        - switch:  切到 next_phase_id，处理 yellow/all-red 过渡

        OnlineController 内部负责：
        1. cycle boundary -> CycleSkill.plan() -> 安全裁剪 -> 设定周期
        2. phase decision point -> PhaseSkill.decide() -> 安全裁剪 -> 微调
        3. phase exhausted -> 自动推进下一相位
        """
        # 第一步：收集所有路口的观测
        all_obs: Dict[str, IntersectionObservation] = {}
        for tls_id in tls_ids:
            current_phase = traci.trafficlight.getPhase(tls_id)
            all_obs[tls_id] = self._build_observation(
                traci, tls_id, tls_data, current_phase
            )

        # 第二步：处理 pending switch durations
        executor.process_pending_switches(tls_ids)

        # 第三步：对每个路口调用 online_controller.step()
        for tls_id in tls_ids:
            cmd = controller.step(tls_id, sim_time, all_obs)

            if cmd is None:
                continue

            # 将 PhaseCommand 映射为 TraCI 操作
            executor.apply(cmd, tls_id)

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
        """从当前 SUMO 状态构建路口观测。

        关于 predicted_arrival:
        - 当 self.enable_prediction=False（默认）时，PhaseObservation.predicted_arrival
          为 DEFAULT_PREDICTED_ARRIVAL（0.0），是一个恒为 0 的占位值。
          Skill 不应依赖此特征做决策。可通过 self.enable_prediction 检查可用性。
        - 当 self.enable_prediction=True 时，将从预测模块获取真实值（TODO）。
        """
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

            # predicted_arrival:
            # - enable_prediction=True 时从预测模块获取（TODO: 接入真实预测）
            # - enable_prediction=False 时使用 DEFAULT_PREDICTED_ARRIVAL（0.0）
            #   skill 不应依赖此特征做决策，因为它是恒为 0 的占位值
            pred_arrival = DEFAULT_PREDICTED_ARRIVAL
            if self.enable_prediction:
                # TODO: 调用预测模块获取真实 predicted_arrival
                pass

            phases_obs[phase_idx] = PhaseObservation(
                phase_id=phase_idx,
                queue=queue,
                waiting_time=wait / max(n, 1),
                predicted_arrival=pred_arrival,
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

            # 跳过全部为 None 的指标
            if all(v is None for v in vals):
                continue

            # 在比较时将 None 视为最差值
            comparable_vals = [v if v is not None else (float('inf') if not higher_better else float('-inf')) for v in vals]
            best_idx = comparable_vals.index(max(comparable_vals) if higher_better else min(comparable_vals))

            line = f"{label:<30}"
            for i, v in enumerate(vals):
                marker = " *" if i == best_idx else "  "
                if v is None:
                    line += f"{'N/A':>12}{marker}"
                elif isinstance(v, float):
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
                        if sc_v is None or bl_v is None or bl_v == 0:
                            continue
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
        # 附加 controller stats（如果有）
        for name, m in self.results.items():
            if m.controller_stats is not None:
                summaries[name]["controller_stats"] = m.controller_stats

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
    # 优先查找 evolution_archive 中的 evolved_cohort，其次查找 skills/cohorts
    evolved_cohort_path = os.path.join(project_dir, "artifacts", "evolution_archive", "evolved_cohort.json")
    if not os.path.exists(evolved_cohort_path):
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
