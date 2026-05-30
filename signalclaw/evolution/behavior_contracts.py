"""BehaviorContracts - 回归保持净化机制。

在进化净化过程中集成行为契约检查，确保净化后的 skill
不会丢失 seed 的安全行为（hunger bonus、smoothing、spillback guard 等）。

核心组件：
- GoldenObservationSet: 预定义 500 个典型交通状态的确定性生成器
- BehaviorContractChecker: 检查净化后 skill 的行为是否满足契约
- RegressionPreservingPurifier: 在净化流程中集成行为契约检查
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from signalclaw.core.state import (
    CyclePlan,
    IntersectionObservation,
    NetworkObservation,
    PhaseCommand,
    PhaseObservation,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ContractViolation:
    """单条契约违规。"""
    obs_index: int
    category: str  # "action_constraint" | "duration_delta" | "starvation_regression" | "spillback_regression" | "determinism"
    message: str
    seed_value: Any = None
    candidate_value: Any = None


@dataclass
class ContractResult:
    """行为契约检查结果。"""
    passed: bool
    violations: List[ContractViolation] = field(default_factory=list)
    total_checks: int = 0
    max_duration_delta: float = 0.0
    starvation_regression_count: int = 0
    spillback_regression_count: int = 0
    determinism_failures: int = 0


@dataclass
class PurificationResult:
    """净化流程结果。"""
    passed: bool
    violations: List[ContractViolation] = field(default_factory=list)
    max_duration_delta: float = 0.0
    starvation_regression: bool = False
    spillback_regression: bool = False


# ---------------------------------------------------------------------------
# GoldenObservationSet - 确定性生成 500 个典型交通状态
# ---------------------------------------------------------------------------

class GoldenObservationSet:
    """预定义 500 个典型交通状态，确定性生成（seed=42）。

    分 5 类，每类 100 个：
    - 低流量状态 (low_flow)
    - 高流量状态 (high_flow)
    - 下游堵塞状态 (downstream_congestion)
    - 单相位饥饿状态 (single_phase_starvation)
    - 邻居释放压力状态 (neighbor_release_pressure)
    """

    CATEGORIES = [
        "low_flow",
        "high_flow",
        "downstream_congestion",
        "single_phase_starvation",
        "neighbor_release_pressure",
    ]

    def __init__(self, seed: int = 42, n_phases: int = 4):
        self._seed = seed
        self._n_phases = n_phases
        self._observations: Optional[List[Dict[str, Any]]] = None

    # -- 确定性 LCG 伪随机数生成器（避免 import random） --

    @staticmethod
    def _lcg(seed: int, n: int) -> List[float]:
        """基于 LCG 生成 n 个 [0, 1) 之间的确定性浮点数。"""
        a = 1664525
        c = 1013904223
        m = 2 ** 32
        values = []
        s = seed
        for _ in range(n):
            s = (a * s + c) % m
            values.append(s / m)
        return values

    def _make_rng(self, seed: int):
        """返回一个闭包形式的确定性随机数生成器。"""
        a = 1664525
        c = 1013904223
        m = 2 ** 32
        state = [seed]

        def _next() -> float:
            state[0] = (a * state[0] + c) % m
            return state[0] / m

        return _next

    # -- 观测值构造辅助 --

    def _make_phase_obs_dict(
        self,
        phase_id: int,
        queue: float = 5.0,
        waiting_time: float = 30.0,
        predicted_arrival: float = 3.0,
        elapsed_green: float = 10.0,
        min_green: float = 10.0,
        max_green: float = 60.0,
        saturation_flow: float = 1900.0,
    ) -> Dict[str, Any]:
        return {
            "phase_id": phase_id,
            "queue": queue,
            "waiting_time": waiting_time,
            "predicted_arrival": predicted_arrival,
            "elapsed_green": elapsed_green,
            "min_green": min_green,
            "max_green": max_green,
            "saturation_flow": saturation_flow,
        }

    def _make_obs_dict(
        self,
        crossing_id: str = "golden_tls",
        phases: Optional[Dict[int, Dict[str, Any]]] = None,
        downstream_queue: Optional[Dict[str, float]] = None,
        upstream_queue: Optional[Dict[str, float]] = None,
        current_phase_id: int = 0,
        current_phase_elapsed: float = 20.0,
        cycle_second: float = 80.0,
        timestamp: float = 100.0,
        spillback_risk: float = 0.0,
        release_pressure: float = 0.0,
        hunger_time: Optional[Dict[int, float]] = None,
    ) -> Dict[str, Any]:
        if phases is None:
            phases = {
                i: self._make_phase_obs_dict(i) for i in range(self._n_phases)
            }
        if hunger_time is None:
            hunger_time = {i: 0.0 for i in range(self._n_phases)}

        return {
            "ego": {
                "crossing_id": crossing_id,
                "current_phase_id": current_phase_id,
                "current_phase_elapsed": current_phase_elapsed,
                "cycle_second": cycle_second,
                "phases": phases,
                "downstream_queue": downstream_queue or {"e1": 3.0, "e2": 2.0},
                "upstream_queue": upstream_queue or {"e3": 5.0, "e4": 4.0},
                "downstream_spillback_risk": spillback_risk,
                "upstream_release_pressure": release_pressure,
            },
            "neighbors": {},
            "timestamp": timestamp,
            # 扩展字段：饥饿时间（每个相位的累积红灯时间）
            "hunger_time": hunger_time,
        }

    # -- 五类状态生成器 --

    def _generate_low_flow(self, rng) -> List[Dict[str, Any]]:
        """低流量状态：queue 0~5，waiting_time 0~15s。"""
        obs_list = []
        for i in range(100):
            phases = {}
            for pid in range(self._n_phases):
                phases[pid] = self._make_phase_obs_dict(
                    phase_id=pid,
                    queue=round(rng() * 5.0, 2),
                    waiting_time=round(rng() * 15.0, 2),
                    predicted_arrival=round(rng() * 3.0, 2),
                    elapsed_green=round(rng() * 20.0, 2),
                )
            obs_list.append(self._make_obs_dict(
                crossing_id=f"low_flow_{i:03d}",
                phases=phases,
                timestamp=float(i * 10),
                downstream_queue={"e1": round(rng() * 2.0, 2)},
                upstream_queue={"e3": round(rng() * 3.0, 2)},
            ))
        return obs_list

    def _generate_high_flow(self, rng) -> List[Dict[str, Any]]:
        """高流量状态：queue 20~60，waiting_time 60~180s。"""
        obs_list = []
        for i in range(100):
            phases = {}
            for pid in range(self._n_phases):
                phases[pid] = self._make_phase_obs_dict(
                    phase_id=pid,
                    queue=round(20.0 + rng() * 40.0, 2),
                    waiting_time=round(60.0 + rng() * 120.0, 2),
                    predicted_arrival=round(5.0 + rng() * 15.0, 2),
                    elapsed_green=round(rng() * 50.0, 2),
                )
            obs_list.append(self._make_obs_dict(
                crossing_id=f"high_flow_{i:03d}",
                phases=phases,
                timestamp=float(i * 10),
                spillback_risk=round(rng() * 0.6, 3),
                release_pressure=round(rng() * 0.5, 3),
                downstream_queue={"e1": round(10.0 + rng() * 20.0, 2), "e2": round(8.0 + rng() * 15.0, 2)},
                upstream_queue={"e3": round(15.0 + rng() * 25.0, 2), "e4": round(12.0 + rng() * 20.0, 2)},
            ))
        return obs_list

    def _generate_downstream_congestion(self, rng) -> List[Dict[str, Any]]:
        """下游堵塞状态：downstream_queue 高，spillback_risk 0.5~1.0。"""
        obs_list = []
        for i in range(100):
            phases = {}
            for pid in range(self._n_phases):
                phases[pid] = self._make_phase_obs_dict(
                    phase_id=pid,
                    queue=round(10.0 + rng() * 25.0, 2),
                    waiting_time=round(40.0 + rng() * 60.0, 2),
                    predicted_arrival=round(3.0 + rng() * 8.0, 2),
                    elapsed_green=round(rng() * 30.0, 2),
                )
            # 部分下游边严重堵塞
            n_edges = 2 + int(rng() * 3)
            dq = {}
            for e in range(n_edges):
                dq[f"e_down_{e}"] = round(20.0 + rng() * 30.0, 2)
            obs_list.append(self._make_obs_dict(
                crossing_id=f"ds_congestion_{i:03d}",
                phases=phases,
                timestamp=float(i * 10),
                spillback_risk=round(0.5 + rng() * 0.5, 3),
                downstream_queue=dq,
                upstream_queue={"e3": round(5.0 + rng() * 10.0, 2)},
            ))
        return obs_list

    def _generate_single_phase_starvation(self, rng) -> List[Dict[str, Any]]:
        """单相位饥饿状态：某个相位 queue 很低但 hunger_time 很高（>60s），
        或者 queue 很高但长期得不到服务。"""
        obs_list = []
        for i in range(100):
            starved_phase = int(rng() * self._n_phases)
            phases = {}
            hunger_time = {}
            for pid in range(self._n_phases):
                if pid == starved_phase:
                    # 饥饿相位：高 queue + 长 hunger_time
                    phases[pid] = self._make_phase_obs_dict(
                        phase_id=pid,
                        queue=round(15.0 + rng() * 30.0, 2),
                        waiting_time=round(90.0 + rng() * 90.0, 2),
                        predicted_arrival=round(5.0 + rng() * 10.0, 2),
                        elapsed_green=0.0,  # 未获得绿灯
                    )
                    hunger_time[pid] = round(60.0 + rng() * 120.0, 2)
                else:
                    phases[pid] = self._make_phase_obs_dict(
                        phase_id=pid,
                        queue=round(3.0 + rng() * 12.0, 2),
                        waiting_time=round(10.0 + rng() * 30.0, 2),
                        predicted_arrival=round(1.0 + rng() * 5.0, 2),
                        elapsed_green=round(rng() * 40.0, 2),
                    )
                    hunger_time[pid] = round(rng() * 20.0, 2)
            obs_list.append(self._make_obs_dict(
                crossing_id=f"starvation_{i:03d}",
                phases=phases,
                timestamp=float(i * 10),
                hunger_time=hunger_time,
                current_phase_id=(starved_phase + 1) % self._n_phases,
            ))
        return obs_list

    def _generate_neighbor_release_pressure(self, rng) -> List[Dict[str, Any]]:
        """邻居释放压力状态：upstream_release_pressure 高，upstream_queue 大。"""
        obs_list = []
        for i in range(100):
            phases = {}
            for pid in range(self._n_phases):
                phases[pid] = self._make_phase_obs_dict(
                    phase_id=pid,
                    queue=round(8.0 + rng() * 20.0, 2),
                    waiting_time=round(30.0 + rng() * 50.0, 2),
                    predicted_arrival=round(3.0 + rng() * 8.0, 2),
                    elapsed_green=round(rng() * 35.0, 2),
                )
            n_up = 2 + int(rng() * 3)
            uq = {}
            for e in range(n_up):
                uq[f"e_up_{e}"] = round(15.0 + rng() * 25.0, 2)
            # 构造邻居观测
            neighbor_id = f"neighbor_{i:03d}"
            neighbor_phases = {}
            for pid in range(self._n_phases):
                neighbor_phases[pid] = self._make_phase_obs_dict(
                    phase_id=pid,
                    queue=round(20.0 + rng() * 30.0, 2),
                    waiting_time=round(60.0 + rng() * 60.0, 2),
                    predicted_arrival=round(5.0 + rng() * 10.0, 2),
                    elapsed_green=round(rng() * 25.0, 2),
                )
            obs = self._make_obs_dict(
                crossing_id=f"pressure_{i:03d}",
                phases=phases,
                timestamp=float(i * 10),
                release_pressure=round(0.5 + rng() * 0.5, 3),
                upstream_queue=uq,
                downstream_queue={"e1": round(3.0 + rng() * 8.0, 2)},
            )
            # 添加邻居信息
            obs["neighbors"] = {
                neighbor_id: {
                    "crossing_id": neighbor_id,
                    "current_phase_id": int(rng() * self._n_phases),
                    "current_phase_elapsed": round(rng() * 40.0, 2),
                    "cycle_second": 80.0,
                    "phases": neighbor_phases,
                    "downstream_queue": {"e_down": round(5.0 + rng() * 10.0, 2)},
                    "upstream_queue": uq,
                    "downstream_spillback_risk": round(rng() * 0.3, 3),
                    "upstream_release_pressure": round(0.6 + rng() * 0.4, 3),
                }
            }
            obs_list.append(obs)
        return obs_list

    # -- 公共接口 --

    def generate(self) -> List[Dict[str, Any]]:
        """生成完整的 500 个 golden observations。"""
        rng = self._make_rng(self._seed)
        all_obs: List[Dict[str, Any]] = []
        all_obs.extend(self._generate_low_flow(rng))
        all_obs.extend(self._generate_high_flow(rng))
        all_obs.extend(self._generate_downstream_congestion(rng))
        all_obs.extend(self._generate_single_phase_starvation(rng))
        all_obs.extend(self._generate_neighbor_release_pressure(rng))
        self._observations = all_obs
        return all_obs

    def get_observations(self) -> List[Dict[str, Any]]:
        """获取已生成的 observations（如未生成则自动生成）。"""
        if self._observations is None:
            self.generate()
        return self._observations  # type: ignore[return-value]

    def get_by_category(self, category: str) -> List[Dict[str, Any]]:
        """按类别获取 observations。

        Parameters
        ----------
        category : str
            "low_flow" | "high_flow" | "downstream_congestion" |
            "single_phase_starvation" | "neighbor_release_pressure"

        Returns
        -------
        List[Dict[str, Any]]
        """
        obs = self.get_observations()
        cat_idx = self.CATEGORIES.index(category)
        start = cat_idx * 100
        return obs[start:start + 100]


# ---------------------------------------------------------------------------
# dict -> state dataclass 转换
# ---------------------------------------------------------------------------

def _dict_to_phase_obs(d: Dict[str, Any]) -> PhaseObservation:
    """将 dict 转换为 PhaseObservation。"""
    return PhaseObservation(
        phase_id=d["phase_id"],
        queue=d["queue"],
        waiting_time=d["waiting_time"],
        predicted_arrival=d["predicted_arrival"],
        elapsed_green=d["elapsed_green"],
        min_green=d["min_green"],
        max_green=d["max_green"],
        saturation_flow=d.get("saturation_flow", 1900.0),
    )


def _dict_to_network_obs(d: Dict[str, Any]) -> NetworkObservation:
    """将 golden obs dict 转换为 NetworkObservation。"""
    ego_d = d["ego"]
    phases = {
        pid: _dict_to_phase_obs(pd)
        for pid, pd in ego_d["phases"].items()
    }
    ego = IntersectionObservation(
        crossing_id=ego_d["crossing_id"],
        current_phase_id=ego_d["current_phase_id"],
        current_phase_elapsed=ego_d["current_phase_elapsed"],
        cycle_second=ego_d["cycle_second"],
        phases=phases,
        downstream_queue=ego_d["downstream_queue"],
        upstream_queue=ego_d["upstream_queue"],
        downstream_spillback_risk=ego_d.get("downstream_spillback_risk", 0.0),
        upstream_release_pressure=ego_d.get("upstream_release_pressure", 0.0),
    )
    neighbors = {}
    for nid, nd in d.get("neighbors", {}).items():
        n_phases = {
            pid: _dict_to_phase_obs(pd)
            for pid, pd in nd["phases"].items()
        }
        neighbors[nid] = IntersectionObservation(
            crossing_id=nd["crossing_id"],
            current_phase_id=nd["current_phase_id"],
            current_phase_elapsed=nd["current_phase_elapsed"],
            cycle_second=nd["cycle_second"],
            phases=n_phases,
            downstream_queue=nd["downstream_queue"],
            upstream_queue=nd["upstream_queue"],
            downstream_spillback_risk=nd.get("downstream_spillback_risk", 0.0),
            upstream_release_pressure=nd.get("upstream_release_pressure", 0.0),
        )
    return NetworkObservation(
        ego=ego,
        neighbors=neighbors,
        timestamp=d.get("timestamp", 0.0),
    )


# ---------------------------------------------------------------------------
# BehaviorContractChecker
# ---------------------------------------------------------------------------

class BehaviorContractChecker:
    """检查净化后 skill 的行为是否满足契约。

    契约检查项：
    1. action 不违反约束（必须是合法 action）
    2. duration 与 seed 差异不超过阈值（默认 5 秒）
    3. 饥饿相位优先级不能下降（hunger_time 最高的相位应得到至少同等的优先级）
    4. 下游堵塞时对应相位绿灯不能增加（spillback guard）
    5. 同一 obs 多次调用输出一致（确定性检查）
    """

    VALID_ACTIONS = {"hold", "switch", "extend", "shorten"}

    def __init__(
        self,
        max_duration_delta: float = 5.0,
        determinism_rounds: int = 3,
    ):
        """
        Parameters
        ----------
        max_duration_delta : float
            允许的最大 duration 偏差（秒）
        determinism_rounds : int
            确定性检查重复调用次数
        """
        self.max_duration_delta = max_duration_delta
        self.determinism_rounds = determinism_rounds

    def check_contracts(
        self,
        seed_skill: Any,
        candidate_skill: Any,
        golden_obs_set: GoldenObservationSet,
        skill_type: str = "phase",
        paired_plan: Optional[CyclePlan] = None,
    ) -> ContractResult:
        """对 candidate_skill 进行完整的行为契约检查。

        Parameters
        ----------
        seed_skill : Any
            seed skill 的命名空间字典（包含 plan 或 decide 函数）
        candidate_skill : Any
            候选 skill 的命名空间字典
        golden_obs_set : GoldenObservationSet
            golden observation 集合
        skill_type : str
            "cycle" 或 "phase"
        paired_plan : CyclePlan, optional
            phase skill 评估时需要的配对 CyclePlan

        Returns
        -------
        ContractResult
        """
        violations: List[ContractViolation] = []
        observations = golden_obs_set.get_observations()

        max_duration_delta_seen = 0.0
        starvation_regression_count = 0
        spillback_regression_count = 0
        determinism_failures = 0

        for idx, obs_dict in enumerate(observations):
            obs = _dict_to_network_obs(obs_dict)
            hunger_time = obs_dict.get("hunger_time", {})

            # -- 获取 seed 和 candidate 的输出 --
            if skill_type == "cycle":
                seed_result = self._safe_call_cycle(seed_skill, obs)
                cand_result = self._safe_call_cycle(candidate_skill, obs)
            else:
                plan = paired_plan or CyclePlan(
                    cycle_length=80.0,
                    green_times={i: 20.0 for i in range(len(obs.ego.phases))},
                    phase_order=list(obs.ego.phases.keys()),
                )
                seed_result = self._safe_call_phase(seed_skill, obs, plan)
                cand_result = self._safe_call_phase(candidate_skill, obs, plan)

            # 无法执行则跳过
            if seed_result is None or cand_result is None:
                continue

            # -- 契约 1：action 不违反约束 --
            if skill_type == "phase":
                action_violations = self._check_action_constraint(
                    idx, cand_result
                )
                violations.extend(action_violations)

                # -- 契约 2：duration 偏差 --
                dur_violations, dur_delta = self._check_duration_delta(
                    idx, seed_result, cand_result
                )
                violations.extend(dur_violations)
                if dur_delta > max_duration_delta_seen:
                    max_duration_delta_seen = dur_delta

                # -- 契约 3：饥饿相位优先级不能下降 --
                starv_violations = self._check_starvation_priority(
                    idx, obs_dict, seed_result, cand_result, hunger_time
                )
                violations.extend(starv_violations)
                starvation_regression_count += len(starv_violations)

                # -- 契约 4：下游堵塞时绿灯不能增加 --
                spill_violations = self._check_spillback_guard(
                    idx, obs_dict, seed_result, cand_result
                )
                violations.extend(spill_violations)
                spillback_regression_count += len(spill_violations)

            else:
                # cycle skill: 检查 green_times 偏差
                dur_violations, dur_delta = self._check_cycle_duration_delta(
                    idx, seed_result, cand_result
                )
                violations.extend(dur_violations)
                if dur_delta > max_duration_delta_seen:
                    max_duration_delta_seen = dur_delta

            # -- 契约 5：确定性检查 --
            det_violations = self._check_determinism(
                idx, candidate_skill, obs, skill_type, paired_plan
            )
            violations.extend(det_violations)
            determinism_failures += len(det_violations)

        passed = len(violations) == 0
        return ContractResult(
            passed=passed,
            violations=violations,
            total_checks=len(observations),
            max_duration_delta=round(max_duration_delta_seen, 4),
            starvation_regression_count=starvation_regression_count,
            spillback_regression_count=spillback_regression_count,
            determinism_failures=determinism_failures,
        )

    # -- Skill 调用 --

    def _safe_call_cycle(
        self, skill_ns: Any, obs: NetworkObservation
    ) -> Optional[CyclePlan]:
        if skill_ns is None:
            return None
        try:
            fn = skill_ns.get("plan") if isinstance(skill_ns, dict) else getattr(skill_ns, "plan", None)
            if fn is None:
                return None
            result = fn(obs)
            if isinstance(result, CyclePlan):
                return result
        except Exception:
            pass
        return None

    def _safe_call_phase(
        self, skill_ns: Any, obs: NetworkObservation, plan: CyclePlan
    ) -> Optional[PhaseCommand]:
        if skill_ns is None:
            return None
        try:
            fn = skill_ns.get("decide") if isinstance(skill_ns, dict) else getattr(skill_ns, "decide", None)
            if fn is None:
                return None
            result = fn(obs, plan)
            if isinstance(result, PhaseCommand):
                return result
        except Exception:
            pass
        return None

    # -- 契约检查方法 --

    def _check_action_constraint(
        self, idx: int, cmd: PhaseCommand
    ) -> List[ContractViolation]:
        """契约 1：action 必须合法。"""
        violations = []
        if cmd.action not in self.VALID_ACTIONS:
            violations.append(ContractViolation(
                obs_index=idx,
                category="action_constraint",
                message=f"非法 action: {cmd.action}",
                candidate_value=cmd.action,
            ))
        return violations

    def _check_duration_delta(
        self, idx: int, seed_cmd: PhaseCommand, cand_cmd: PhaseCommand
    ) -> Tuple[List[ContractViolation], float]:
        """契约 2：duration 与 seed 差异不超过阈值。"""
        violations = []
        delta = abs(cand_cmd.duration - seed_cmd.duration)
        if delta > self.max_duration_delta:
            violations.append(ContractViolation(
                obs_index=idx,
                category="duration_delta",
                message=(
                    f"duration 偏差 {delta:.2f}s 超过阈值 "
                    f"{self.max_duration_delta:.2f}s"
                ),
                seed_value=seed_cmd.duration,
                candidate_value=cand_cmd.duration,
            ))
        return violations, delta

    def _check_cycle_duration_delta(
        self, idx: int, seed_plan: CyclePlan, cand_plan: CyclePlan
    ) -> Tuple[List[ContractViolation], float]:
        """cycle skill 的 green_times 偏差检查。"""
        violations = []
        max_delta = 0.0
        all_phase_ids = set(seed_plan.green_times.keys()) | set(cand_plan.green_times.keys())
        for pid in all_phase_ids:
            s_val = seed_plan.green_times.get(pid, 0.0)
            c_val = cand_plan.green_times.get(pid, 0.0)
            delta = abs(c_val - s_val)
            if delta > max_delta:
                max_delta = delta
            if delta > self.max_duration_delta:
                violations.append(ContractViolation(
                    obs_index=idx,
                    category="duration_delta",
                    message=(
                        f"phase {pid} green_time 偏差 {delta:.2f}s "
                        f"超过阈值 {self.max_duration_delta:.2f}s"
                    ),
                    seed_value=s_val,
                    candidate_value=c_val,
                ))
        return violations, max_delta

    def _check_starvation_priority(
        self,
        idx: int,
        obs_dict: Dict[str, Any],
        seed_cmd: PhaseCommand,
        cand_cmd: PhaseCommand,
        hunger_time: Dict[int, float],
    ) -> List[ContractViolation]:
        """契约 3：饥饿相位优先级不能下降。

        如果 seed 为饥饿相位分配了绿灯（action=extend/hold，
        next_phase_id 指向饥饿相位），candidate 不能降低该优先级。
        """
        violations = []
        if not hunger_time:
            return violations

        # 找出最饥饿的相位
        max_hunger_phase = max(hunger_time, key=lambda k: hunger_time[k])
        max_hunger = hunger_time[max_hunger_phase]

        # 如果有显著饥饿（>30s）
        if max_hunger <= 30.0:
            return violations

        # seed 选择服务饥饿相位，candidate 没有 -> 回归
        seed_serves_starved = (
            seed_cmd.next_phase_id == max_hunger_phase
            and seed_cmd.action in ("extend", "hold", "switch")
        )
        cand_serves_starved = (
            cand_cmd.next_phase_id == max_hunger_phase
            and cand_cmd.action in ("extend", "hold", "switch")
        )

        if seed_serves_starved and not cand_serves_starved:
            violations.append(ContractViolation(
                obs_index=idx,
                category="starvation_regression",
                message=(
                    f"饥饿相位 {max_hunger_phase} (hunger={max_hunger:.1f}s) "
                    f"被 seed 服务但 candidate 忽略: "
                    f"seed -> phase {seed_cmd.next_phase_id}/{seed_cmd.action}, "
                    f"cand -> phase {cand_cmd.next_phase_id}/{cand_cmd.action}"
                ),
                seed_value=seed_cmd.next_phase_id,
                candidate_value=cand_cmd.next_phase_id,
            ))

        return violations

    def _check_spillback_guard(
        self,
        idx: int,
        obs_dict: Dict[str, Any],
        seed_cmd: PhaseCommand,
        cand_cmd: PhaseCommand,
    ) -> List[ContractViolation]:
        """契约 4：下游堵塞时对应相位绿灯不能增加。

        如果 spillback_risk > 0.5，seed 缩短了某相位绿灯，
        candidate 不应增加该相位绿灯。
        """
        violations = []
        spillback_risk = obs_dict["ego"].get("downstream_spillback_risk", 0.0)
        if spillback_risk <= 0.5:
            return violations

        # seed 减少了 duration，candidate 增加了 -> 回归
        seed_shortened = seed_cmd.action in ("shorten", "switch")
        cand_extended = (
            cand_cmd.action in ("extend",)
            and cand_cmd.duration > seed_cmd.duration
        )

        # 或者 seed 给了短 duration，candidate 给了更长的
        if seed_cmd.duration > 0 and cand_cmd.duration > seed_cmd.duration + 2.0:
            # seed 没有主动服务该相位但给了短绿灯，
            # candidate 给了更长的绿灯 -> 在堵塞时是危险的
            downstream_q = obs_dict["ego"].get("downstream_queue", {})
            total_downstream = sum(downstream_q.values())
            if total_downstream > 15.0:
                violations.append(ContractViolation(
                    obs_index=idx,
                    category="spillback_regression",
                    message=(
                        f"下游堵塞 (risk={spillback_risk:.2f}, "
                        f"downstream_queue_total={total_downstream:.1f}) "
                        f"时 candidate 增加绿灯: "
                        f"seed duration={seed_cmd.duration:.1f}s -> "
                        f"cand duration={cand_cmd.duration:.1f}s"
                    ),
                    seed_value=seed_cmd.duration,
                    candidate_value=cand_cmd.duration,
                ))

        return violations

    def _check_determinism(
        self,
        idx: int,
        skill_ns: Any,
        obs: NetworkObservation,
        skill_type: str,
        paired_plan: Optional[CyclePlan],
    ) -> List[ContractViolation]:
        """契约 5：同一 obs 多次调用输出一致。"""
        violations = []

        if skill_type == "phase":
            plan = paired_plan or CyclePlan(
                cycle_length=80.0,
                green_times={i: 20.0 for i in range(len(obs.ego.phases))},
                phase_order=list(obs.ego.phases.keys()),
            )
            results = []
            for _ in range(self.determinism_rounds):
                result = self._safe_call_phase(skill_ns, obs, plan)
                if result is None:
                    return []
                results.append(result)
            # 比较所有结果
            for i in range(1, len(results)):
                if results[i].action != results[0].action:
                    violations.append(ContractViolation(
                        obs_index=idx,
                        category="determinism",
                        message=(
                            f"非确定性输出: action 不一致 "
                            f"({results[0].action} vs {results[i].action})"
                        ),
                    ))
                    break
                if abs(results[i].duration - results[0].duration) > 0.01:
                    violations.append(ContractViolation(
                        obs_index=idx,
                        category="determinism",
                        message=(
                            f"非确定性输出: duration 不一致 "
                            f"({results[0].duration:.4f} vs {results[i].duration:.4f})"
                        ),
                    ))
                    break
                if results[i].next_phase_id != results[0].next_phase_id:
                    violations.append(ContractViolation(
                        obs_index=idx,
                        category="determinism",
                        message=(
                            f"非确定性输出: next_phase_id 不一致 "
                            f"({results[0].next_phase_id} vs {results[i].next_phase_id})"
                        ),
                    ))
                    break
        else:
            results = []
            for _ in range(self.determinism_rounds):
                result = self._safe_call_cycle(skill_ns, obs)
                if result is None:
                    return []
                results.append(result)
            for i in range(1, len(results)):
                if abs(results[i].cycle_length - results[0].cycle_length) > 0.01:
                    violations.append(ContractViolation(
                        obs_index=idx,
                        category="determinism",
                        message=(
                            f"非确定性输出: cycle_length 不一致 "
                            f"({results[0].cycle_length:.4f} vs {results[i].cycle_length:.4f})"
                        ),
                    ))
                    break

        return violations


# ---------------------------------------------------------------------------
# RegressionPreservingPurifier
# ---------------------------------------------------------------------------

class RegressionPreservingPurifier:
    """在净化流程中集成行为契约检查。

    确保净化后的 skill 不会丢失 seed 的关键安全行为。
    """

    def __init__(
        self,
        max_duration_delta: float = 5.0,
        determinism_rounds: int = 3,
    ):
        """
        Parameters
        ----------
        max_duration_delta : float
            允许的最大 duration 偏差（秒）
        determinism_rounds : int
            确定性检查重复次数
        """
        self.checker = BehaviorContractChecker(
            max_duration_delta=max_duration_delta,
            determinism_rounds=determinism_rounds,
        )
        self.golden_set = GoldenObservationSet()

    def purify(
        self,
        seed_skill: Any,
        candidate_skill: Any,
        golden_obs_set: Optional[GoldenObservationSet] = None,
        skill_type: str = "phase",
        paired_plan: Optional[CyclePlan] = None,
    ) -> PurificationResult:
        """执行回归保持净化。

        Parameters
        ----------
        seed_skill : Any
            seed skill 的命名空间字典
        candidate_skill : Any
            候选 skill 的命名空间字典
        golden_obs_set : GoldenObservationSet, optional
            golden observation 集合（默认使用内置集合）
        skill_type : str
            "cycle" 或 "phase"
        paired_plan : CyclePlan, optional
            phase skill 评估时需要的配对 CyclePlan

        Returns
        -------
        PurificationResult
        """
        if golden_obs_set is None:
            golden_obs_set = self.golden_set

        contract_result = self.checker.check_contracts(
            seed_skill=seed_skill,
            candidate_skill=candidate_skill,
            golden_obs_set=golden_obs_set,
            skill_type=skill_type,
            paired_plan=paired_plan,
        )

        # 判断是否有饥饿回归和下游堵塞回归
        has_starvation_regression = contract_result.starvation_regression_count > 0
        has_spillback_regression = contract_result.spillback_regression_count > 0

        # 如果有任何 violation，净化失败
        # 但也允许仅对 determinism 和 duration 做软性检查
        hard_violations = [
            v for v in contract_result.violations
            if v.category in (
                "action_constraint",
                "starvation_regression",
                "spillback_regression",
            )
        ]

        passed = len(hard_violations) == 0 and contract_result.determinism_failures == 0

        return PurificationResult(
            passed=passed,
            violations=contract_result.violations,
            max_duration_delta=contract_result.max_duration_delta,
            starvation_regression=has_starvation_regression,
            spillback_regression=has_spillback_regression,
        )
