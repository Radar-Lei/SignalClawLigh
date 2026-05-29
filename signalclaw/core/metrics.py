from dataclasses import dataclass, field
from typing import Dict, List
import time

@dataclass
class StepMetrics:
    tls_id: str
    step: int
    sim_time: float
    phase_id: int
    queue_total: float
    waiting_time_avg: float
    throughput: float  # vehicles passed in this step
    delay_total: float
    stops: int

@dataclass
class SimulationMetrics:
    method_name: str
    total_sim_time: float = 0.0
    steps: int = 0
    travel_times: List[float] = field(default_factory=list)
    step_metrics: Dict[str, List[StepMetrics]] = field(default_factory=dict)

    def add_step(self, m: StepMetrics):
        if m.tls_id not in self.step_metrics:
            self.step_metrics[m.tls_id] = []
        self.step_metrics[m.tls_id].append(m)
        self.steps += 1

    def summary(self) -> dict:
        all_queues = []
        all_waits = []
        all_delays = []
        all_throughputs = []
        all_stops = []
        for tls_metrics in self.step_metrics.values():
            for m in tls_metrics:
                all_queues.append(m.queue_total)
                all_waits.append(m.waiting_time_avg)
                all_delays.append(m.delay_total)
                all_throughputs.append(m.throughput)
                all_stops.append(m.stops)

        n = max(len(all_queues), 1)
        return {
            "method": self.method_name,
            "avg_queue": sum(all_queues) / n,
            "max_queue": max(all_queues) if all_queues else 0,
            "avg_waiting_time": sum(all_waits) / n,
            "max_waiting_time": max(all_waits) if all_waits else 0,
            "avg_delay": sum(all_delays) / n,
            "total_throughput": sum(all_throughputs),
            "total_stops": sum(all_stops),
            "avg_travel_time": sum(self.travel_times) / max(len(self.travel_times), 1),
            "completed_vehicles": len(self.travel_times),
            "steps": self.steps,
        }
