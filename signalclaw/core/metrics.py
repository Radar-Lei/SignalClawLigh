from dataclasses import dataclass, field
from typing import Dict, List, Optional
import time

@dataclass
class StepMetrics:
    tls_id: str
    step: int
    sim_time: float
    phase_id: int
    queue_total: float
    waiting_time_avg: float
    throughput: float  # vehicles completed in this sampling interval
    delay_total: float
    stops: int

@dataclass
class SimulationMetrics:
    method_name: str
    total_sim_time: float = 0.0
    steps: int = 0
    travel_times: List[float] = field(default_factory=list)
    waiting_times: List[float] = field(default_factory=list)
    total_stops_from_tripinfo: Optional[int] = None
    step_metrics: Dict[str, List[StepMetrics]] = field(default_factory=dict)
    # TripInfoCollector 计算的时间加权平均 queue（真实数据，每步每 lane halting 的时间平均）
    # 优先级高于 step_metrics 中的采样平均。
    _collector_avg_queue: Optional[float] = field(default=None, repr=False)
    # OnlineController 统计日志（仅 OnlineController 方法有值）
    controller_stats: Optional[dict] = None

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
        for tls_metrics in self.step_metrics.values():
            for m in tls_metrics:
                all_queues.append(m.queue_total)
                all_waits.append(m.waiting_time_avg)
                all_delays.append(m.delay_total)
                all_throughputs.append(m.throughput)

        n = max(len(all_queues), 1)

        # completed_vehicles: 优先使用 travel_times 长度（tripinfo 或 fallback）
        completed_vehicles = len(self.travel_times)

        # avg_travel_time: 使用完成车辆的真实 travel time 均值
        avg_travel_time = (sum(self.travel_times) / completed_vehicles
                           if completed_vehicles > 0 else 0.0)

        # avg_waiting_time: 优先使用 tripinfo waitingTime 均值，否则使用 lane-level sample
        has_tripinfo_wt = bool(self.waiting_times)
        if has_tripinfo_wt:
            avg_waiting_time = sum(self.waiting_times) / max(len(self.waiting_times), 1)
            waiting_time_source = "tripinfo"
        else:
            avg_waiting_time = sum(all_waits) / n
            waiting_time_source = "lane_sample"

        # total_stops: 优先使用 TripInfoCollector 的逐步累加真实 halting number，
        #              其次使用 tripinfo XML 中的 stops，否则标记 N/A
        if self.total_stops_from_tripinfo is not None:
            total_stops = self.total_stops_from_tripinfo
            stops_source = "tripinfo_collector"  # 来自 TripInfoCollector 逐步累加
        else:
            total_stops = None  # N/A — 不再使用 proxy
            stops_source = "N/A"

        # avg_queue: 优先使用 TripInfoCollector 的时间加权平均（真实数据），
        #            其次使用 step_metrics 中的采样平均（近似值）
        if self._collector_avg_queue is not None:
            avg_queue = self._collector_avg_queue
            queue_source = "collector_time_weighted"  # 真实: 每步每 lane halting 的时间加权平均
        else:
            avg_queue = sum(all_queues) / n  # 近似: 每 10 步采样平均
            queue_source = "step_sample"

        # throughput_per_hour: completed_vehicles / simulated_hours
        simulated_hours = max(self.total_sim_time / 3600.0, 1e-9)
        throughput_per_hour = completed_vehicles / simulated_hours

        return {
            "method": self.method_name,
            "avg_travel_time": avg_travel_time,
            "completed_vehicles": completed_vehicles,
            "throughput_per_hour": throughput_per_hour,
            "avg_queue": avg_queue,
            "avg_queue_source": queue_source,
            "max_queue": max(all_queues) if all_queues else 0,
            "avg_waiting_time": avg_waiting_time,
            "waiting_time_source": waiting_time_source,
            "max_waiting_time": max(all_waits) if all_waits else 0,
            "avg_delay": sum(all_delays) / n,
            "total_throughput": sum(all_throughputs),
            "total_stops": total_stops,
            "stops_source": stops_source,
            "steps": self.steps,
        }
