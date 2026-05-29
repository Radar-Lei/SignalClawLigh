#!/usr/bin/env python3
"""
Experiment runner for comparing traffic signal control methods.
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
    CyclePlan, PhaseCommand, PhaseObservation
)
from signalclaw.core.metrics import StepMetrics, SimulationMetrics
from signalclaw.skills.max_pressure import MaxPressureSkill
from signalclaw.skills.signalclaw_skill import SignalClawSkill


def is_green_phase(state_str: str) -> bool:
    """Check if a phase state string represents a green phase (has g/G but no y)"""
    has_green = any(c in 'gG' for c in state_str)
    has_yellow = any(c in 'y' for c in state_str)
    return has_green and not has_yellow


class ExperimentRunner:
    """Run traffic signal control experiments in SUMO"""

    def __init__(self, sumocfg_path: str, seed: int = 42,
                 decision_interval: float = 1.0,
                 step_length: float = 1.0):
        self.sumocfg_path = sumocfg_path
        self.seed = seed
        self.decision_interval = decision_interval
        self.step_length = step_length
        self.results: Dict[str, SimulationMetrics] = {}

    def _run_simulation(self, method_name: str, skill,
                        verbose: bool = True) -> SimulationMetrics:
        """Run a single method"""
        import traci
        import sumolib

        metrics = SimulationMetrics(method_name=method_name)

        # Start SUMO
        cmd = ["sumo", "-c", self.sumocfg_path,
               "--seed", str(self.seed),
               "--step-length", str(self.step_length),
               "--no-warnings", "--no-step-log",
               "--time-to-teleport", "-1"]

        traci.start(cmd)

        # Parse network to build TLS info
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

            # Identify green phases and their incoming/outgoing edges
            green_info = []  # list of (phase_index, incoming_edges, outgoing_edges)
            connections = tls.getConnections()

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

            tls_data[tls_id] = {
                'phases': phases,
                'green_info': green_info,  # (phase_idx, in_edges, out_edges)
                'green_indices': [gi[0] for gi in green_info],
                'num_phases': len(phases),
                'default_durations': [ph.duration for ph in phases],
            }

        tls_ids = list(tls_data.keys())
        if verbose:
            print(f"  [{method_name}] Found {len(tls_ids)} traffic lights")

        # Track vehicles
        vehicle_depart = {}
        vehicle_arrived = []

        # Per-TLS control state
        tls_state = {}
        for tls_id in tls_ids:
            td = tls_data[tls_id]
            tls_state[tls_id] = {
                'last_green_phase': None,  # Last green phase we set duration for
                'cycle_count': 0,
                'green_durations': {},  # phase_idx -> planned duration
            }

        if skill is not None:
            skill.reset()

        step = 0
        sim_time = 0.0

        # Main simulation loop
        while sim_time < 3600:
            traci.simulationStep()
            step += 1
            sim_time = traci.simulation.getTime()

            # Track vehicles
            for veh_id in traci.simulation.getDepartedIDList():
                vehicle_depart[veh_id] = sim_time
            for veh_id in traci.simulation.getArrivedIDList():
                if veh_id in vehicle_depart:
                    vehicle_arrived.append(sim_time - vehicle_depart[veh_id])
                    del vehicle_depart[veh_id]

            # Control traffic lights
            if skill is not None:
                for tls_id in tls_ids:
                    td = tls_data[tls_id]
                    state = tls_state[tls_id]
                    current_phase = traci.trafficlight.getPhase(tls_id)

                    # Only act when we just entered a green phase
                    # Check if current phase is green and we haven't set its duration yet
                    is_green = is_green_phase(td['phases'][current_phase].state)

                    if is_green and state['last_green_phase'] != current_phase:
                        state['last_green_phase'] = current_phase

                        # Build observation for this intersection
                        obs = self._build_observation(
                            traci, tls_id, tls_data, current_phase
                        )

                        neighbors = {}
                        net_obs = NetworkObservation(
                            ego=obs, neighbors=neighbors, timestamp=sim_time
                        )

                        # Get planned duration from skill
                        plan = skill.plan_cycle(net_obs)

                        if plan is not None:
                            # Find which green phase this corresponds to in the plan
                            green_idx_in_plan = None
                            for i, gi in enumerate(td['green_info']):
                                if gi[0] == current_phase:
                                    green_idx_in_plan = i
                                    break

                            if green_idx_in_plan is not None and green_idx_in_plan < len(plan.phase_order):
                                planned_phase = plan.phase_order[green_idx_in_plan]
                                duration = plan.green_times.get(planned_phase, td['default_durations'][current_phase])
                            else:
                                duration = td['default_durations'][current_phase]

                            # Clamp to reasonable range
                            duration = max(8.0, min(90.0, duration))

                            # Set the phase duration
                            traci.trafficlight.setPhaseDuration(tls_id, duration)
                            state['green_durations'][current_phase] = duration
                        else:
                            # No plan - use default duration
                            pass

            # Collect metrics every 10 steps
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

    def _build_observation(self, traci, tls_id: str, tls_data: dict,
                           current_phase: int) -> IntersectionObservation:
        """Build intersection observation from current SUMO state"""
        td = tls_data[tls_id]

        phases_obs = {}
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

            down_q = {}
            for edge in out_edges:
                try:
                    q = sum(traci.lane.getLastStepHaltingNumber(f"{edge}_{li}")
                            for li in range(traci.edge.getLaneNumber(edge)))
                    down_q[edge] = q
                except traci.exceptions.TraCIException:
                    pass

            phases_obs[phase_idx] = PhaseObservation(
                phase_id=phase_idx,
                queue=queue,
                waiting_time=wait / max(n, 1),
                predicted_arrival=0.0,
                elapsed_green=0.0,
                min_green=8.0,
                max_green=90.0,
            )

        return IntersectionObservation(
            crossing_id=tls_id,
            current_phase_id=current_phase,
            current_phase_elapsed=0.0,
            cycle_second=0.0,
            phases=phases_obs,
            downstream_queue={},
            upstream_queue={},
        )

    def run_all(self, methods: Dict[str, Any] = None, verbose: bool = True):
        if methods is None:
            methods = {
                "FixedTime": None,
                "MaxPressure": MaxPressureSkill(decision_interval=5.0),
                "SignalClaw": SignalClawSkill(decision_interval=5.0),
            }

        for name, skill in methods.items():
            if verbose:
                print(f"\n{'='*60}")
                print(f"Running method: {name}")
                print(f"{'='*60}")
            self.results[name] = self._run_simulation(name, skill, verbose=verbose)

        return self.results

    def print_comparison(self):
        print(f"\n{'='*80}")
        print(f"{'EXPERIMENT RESULTS':^80}")
        print(f"{'='*80}")
        print(f"{'Metric':<30} {'FixedTime':>15} {'MaxPressure':>15} {'SignalClaw':>15}")
        print(f"{'-'*80}")

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
            for name in ["FixedTime", "MaxPressure", "SignalClaw"]:
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

        print(f"{'-'*80}")
        print(f"* = best value")

        sc = summaries.get("SignalClaw", {})
        mp = summaries.get("MaxPressure", {})
        if sc and mp:
            print(f"\n{'SignalClaw vs MaxPressure':^80}")
            print(f"{'-'*80}")
            for label, key, higher_better in metrics_list:
                sc_v = sc.get(key, 0)
                mp_v = mp.get(key, 0)
                if mp_v != 0:
                    pct = (sc_v - mp_v) / abs(mp_v) * 100
                    if higher_better:
                        better = "BETTER" if pct > 0 else "worse"
                    else:
                        better = "BETTER" if pct < 0 else "worse"
                    print(f"  {label:<30}: {pct:+.1f}% ({better})")

    def save_results(self, output_dir: str = "results"):
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
                                        sm.waiting_time_avg, sm.throughput, sm.delay_total, sm.stops])

        print(f"Results saved to {output_dir}/")


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(os.path.dirname(script_dir))
    sumocfg_path = os.path.join(project_dir, "sumo_scenarios", "chengdu", "chengdu.sumocfg")

    print(f"SUMO config: {sumocfg_path}")
    print(f"Simulation time: 3600s")

    runner = ExperimentRunner(sumocfg_path=sumocfg_path, seed=42)
    runner.run_all()
    runner.print_comparison()
    runner.save_results(os.path.join(project_dir, "results"))


if __name__ == "__main__":
    main()
