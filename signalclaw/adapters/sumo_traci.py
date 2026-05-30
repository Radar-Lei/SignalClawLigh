import sys
import os

# 确保 SUMO tools 在 sys.path 中（支持非标准安装位置）
_sumo_home = os.environ.get("SUMO_HOME", "/usr/share/sumo")
_sumo_tools = os.path.join(_sumo_home, "tools")
if _sumo_tools not in sys.path:
    sys.path.insert(0, _sumo_tools)

import traci
import sumolib
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Any
from signalclaw.core.state import (
    PhaseObservation, IntersectionObservation,
    NetworkObservation, CyclePlan, PhaseCommand
)


class SumoTraCIAdapter:
    """Manages SUMO simulation via TraCI"""

    def __init__(self, sumocfg_path: str, use_gui: bool = False,
                 seed: int = 42, step_length: float = 1.0):
        self.sumocfg_path = sumocfg_path
        self.use_gui = use_gui
        self.seed = seed
        self.step_length = step_length
        self._conn = None
        self._tls_info = {}  # tls_id -> TLSInfo
        self._step = 0

    def start(self):
        """Start SUMO simulation"""
        if self.use_gui:
            cmd = ["sumo-gui", "-c", self.sumocfg_path]
        else:
            cmd = ["sumo", "-c", self.sumocfg_path]
        cmd.extend(["--seed", str(self.seed), "--step-length", str(self.step_length)])
        cmd.extend(["--no-warnings", "--no-step-log"])
        traci.start(cmd)
        self._conn = traci
        self._parse_network()

    def _parse_network(self):
        """Parse the network to build TLS phase mapping"""
        net = sumolib.net.readNet(
            os.path.join(os.path.dirname(self.sumocfg_path), "chengdu.net.xml"),
            withPrograms=True, withConnections=True, withInternal=True
        )
        for tls in net.getTrafficLights():
            tls_id = tls.getID()
            programs = tls.getPrograms()
            # Get the default program (usually "0" or the first one)
            if programs:
                prog = list(programs.values())[0]
                phases = prog.getPhases()
                # Identify green phases (not all red, not yellow)
                green_phases = []
                yellow_phases = []
                for i, phase in enumerate(phases):
                    state = phase.state
                    # Check if it's a green phase (has 'g' or 'G')
                    has_green = any(c in 'gG' for c in state)
                    has_yellow = any(c in 'y' for c in state)
                    has_red = all(c in 'rR' for c in state)

                    if has_green and not has_yellow:
                        green_phases.append(i)
                    elif has_yellow or has_red:
                        yellow_phases.append(i)

                # Build incoming/outgoing edge mapping for each green phase
                edges_in = tls.getEdges()
                connections = tls.getConnections()

                # For each green phase, determine which incoming lanes get green
                phase_incoming = defaultdict(set)
                phase_outgoing = defaultdict(set)

                for conn in connections:
                    # conn is [inLane, outLane, linkIndex]
                    in_edge = conn[0].getEdge().getID()
                    out_edge = conn[1].getEdge().getID()
                    link_idx = conn[2] if len(conn) > 2 else 0

                    for gp in green_phases:
                        state = phases[gp].state
                        if link_idx < len(state) and state[link_idx] in 'gG':
                            phase_incoming[gp].add(in_edge)
                            phase_outgoing[gp].add(out_edge)

                self._tls_info[tls_id] = {
                    'green_phases': green_phases,
                    'yellow_phases': yellow_phases,
                    'all_phases': list(range(len(phases))),
                    'phase_states': {i: phases[i].state for i in range(len(phases))},
                    'phase_durations': {i: phases[i].duration for i in range(len(phases))},
                    'phase_incoming': dict(phase_incoming),
                    'phase_outgoing': dict(phase_outgoing),
                    'num_phases': len(phases),
                }

    def get_tls_ids(self) -> List[str]:
        """Get all traffic light IDs"""
        return list(self._tls_info.keys())

    def get_tls_info(self, tls_id: str) -> dict:
        return self._tls_info[tls_id]

    def observe_intersection(self, tls_id: str) -> IntersectionObservation:
        """Get current state observation for an intersection"""
        info = self._tls_info[tls_id]
        current_phase = self._conn.tls.getPhase(tls_id)
        current_program = self._conn.tls.getProgram(tls_id)

        # Get elapsed time in current phase
        # TraCI doesn't directly give elapsed time, we compute from next switch
        remaining = self._conn.tls.getNextSwitch(tls_id) - self._conn.simulation.getTime()
        phase_duration = info['phase_durations'].get(current_phase, 30.0)
        elapsed = phase_duration - remaining

        phases_obs = {}
        for gp in info['green_phases']:
            in_edges = info['phase_incoming'].get(gp, set())
            out_edges = info['phase_outgoing'].get(gp, set())

            # Count vehicles on incoming edges
            queue = 0.0
            wait_time = 0.0
            n_edges = 0
            for edge in in_edges:
                lane_ids = self._conn.edge.getLaneIDs(edge) if hasattr(self._conn.edge, 'getLaneIDs') else [f"{edge}_{i}" for i in range(self._conn.edge.getLaneNumber(edge))]
                for lane in lane_ids:
                    queue += self._conn.lane.getLastStepHaltingNumber(lane)
                    wait_time += self._conn.lane.getWaitingTime(lane)
                    n_edges += 1

            # Count vehicles on outgoing edges
            downstream_q = 0.0
            for edge in out_edges:
                lane_ids = [f"{edge}_{i}" for i in range(self._conn.edge.getLaneNumber(edge))]
                for lane in lane_ids:
                    downstream_q += self._conn.lane.getLastStepHaltingNumber(lane)

            phases_obs[gp] = PhaseObservation(
                phase_id=gp,
                queue=queue,
                waiting_time=wait_time / max(n_edges, 1),
                predicted_arrival=0.0,  # Will be filled by prediction module
                elapsed_green=elapsed if gp == current_phase else 0.0,
                min_green=10.0,
                max_green=60.0,
                saturation_flow=1900.0,
            )

        # Calculate downstream/upstream pressure
        all_downstream_q = {}
        all_upstream_q = {}
        for gp in info['green_phases']:
            for edge in info['phase_outgoing'].get(gp, set()):
                lane_ids = [f"{edge}_{i}" for i in range(self._conn.edge.getLaneNumber(edge))]
                for lane in lane_ids:
                    all_downstream_q[edge] = self._conn.lane.getLastStepHaltingNumber(lane)
            for edge in info['phase_incoming'].get(gp, set()):
                lane_ids = [f"{edge}_{i}" for i in range(self._conn.edge.getLaneNumber(edge))]
                for lane in lane_ids:
                    all_upstream_q[edge] = self._conn.lane.getLastStepHaltingNumber(lane)

        return IntersectionObservation(
            crossing_id=tls_id,
            current_phase_id=current_phase,
            current_phase_elapsed=elapsed,
            cycle_second=0.0,
            phases=phases_obs,
            downstream_queue=all_downstream_q,
            upstream_queue=all_upstream_q,
            downstream_spillback_risk=0.0,
            upstream_release_pressure=0.0,
        )

    def observe_network(self) -> Dict[str, IntersectionObservation]:
        """Observe all intersections"""
        result = {}
        for tls_id in self._tls_info:
            result[tls_id] = self.observe_intersection(tls_id)
        return result

    def build_network_observation(self, tls_id: str, all_obs: Dict[str, IntersectionObservation]) -> NetworkObservation:
        """Build NetworkObservation for a single TLS"""
        ego = all_obs[tls_id]
        # Find neighbors (for now, all other TLS are potential neighbors)
        # In a real system, this would use the graph structure
        neighbors = {k: v for k, v in all_obs.items() if k != tls_id}
        return NetworkObservation(
            ego=ego,
            neighbors=neighbors,
            timestamp=self._conn.simulation.getTime(),
        )

    def set_phase(self, tls_id: str, phase_index: int, duration: float = None):
        """Set traffic light to a specific phase"""
        self._conn.tls.setPhase(tls_id, phase_index)
        if duration is not None:
            self._conn.tls.setPhaseDuration(tls_id, duration)

    def extend_current_phase(self, tls_id: str, additional_seconds: float):
        """Extend current phase duration"""
        self._conn.tls.setPhaseDuration(tls_id, additional_seconds)

    def step(self) -> float:
        """Advance simulation by one step"""
        self._conn.simulationStep()
        self._step += 1
        return self._conn.simulation.getTime()

    def get_sim_time(self) -> float:
        return self._conn.simulation.getTime()

    def get_step(self) -> int:
        return self._step

    def get_departed_vehicles(self) -> List[str]:
        return self._conn.simulation.getDepartedIDList()

    def get_arrived_vehicles(self) -> List[str]:
        return self._conn.simulation.getArrivedIDList()

    def get_intersection_throughput(self, tls_id: str) -> int:
        """返回本步通过指定路口 outgoing edges 的移动车辆总数。

        通过统计目标 TLS 所有 outgoing edges 上的移动车辆数
        （总车辆数 - 静止车辆数）来近似衡量路口的实际通过量。
        比 get_departed_vehicles() 更精确，因为它只计算真正经过
        目标路口的车辆，而非全网出发车辆。
        """
        if tls_id not in self._tls_info:
            return 0

        info = self._tls_info[tls_id]
        # 收集所有 green phase 的 outgoing edges（去重）
        all_outgoing: set = set()
        for gp in info['green_phases']:
            all_outgoing.update(info['phase_outgoing'].get(gp, set()))

        if not all_outgoing:
            return 0

        # 统计 outgoing edges 上的移动车辆数（总车辆 - 静止车辆）
        moving_count = 0
        for edge_id in all_outgoing:
            try:
                n_veh = self._conn.edge.getLastStepVehicleNumber(edge_id)
                n_halting = 0
                for i in range(self._conn.edge.getLaneNumber(edge_id)):
                    lane_id = f"{edge_id}_{i}"
                    n_halting += self._conn.lane.getLastStepHaltingNumber(lane_id)
                moving_count += max(n_veh - n_halting, 0)
            except Exception:
                pass

        return moving_count

    def get_vehicle_travel_time(self, veh_id: str) -> float:
        """Get travel time for a vehicle"""
        try:
            return self._conn.vehicle.getRouteLength(veh_id)
        except traci.exceptions.TraCIException:
            return 0.0

    def get_total_queue(self) -> float:
        """Get total queue across all managed TLS"""
        total = 0
        for tls_id in self._tls_info:
            for gp in self._tls_info[tls_id]['green_phases']:
                for edge in self._tls_info[tls_id]['phase_incoming'].get(gp, set()):
                    lane_ids = [f"{edge}_{i}" for i in range(self._conn.edge.getLaneNumber(edge))]
                    for lane in lane_ids:
                        total += self._conn.lane.getLastStepHaltingNumber(lane)
        return total

    def get_total_waiting_time(self) -> float:
        """Get total waiting time across all managed TLS"""
        total = 0.0
        for tls_id in self._tls_info:
            for gp in self._tls_info[tls_id]['green_phases']:
                for edge in self._tls_info[tls_id]['phase_incoming'].get(gp, set()):
                    lane_ids = [f"{edge}_{i}" for i in range(self._conn.edge.getLaneNumber(edge))]
                    for lane in lane_ids:
                        total += self._conn.lane.getWaitingTime(lane)
        return total

    def close(self):
        """Close TraCI connection"""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()
