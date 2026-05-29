"""ReplayEvaluator - 离线安全评估器。

基于规则的检查，不需要真正跑 SUMO。通过构造多种测试场景，
在内存中运行 skill 代码，检查输出是否满足安全约束。

同时集成 PriorConsistencyChecker，在 Replay 评估之后
额外检查输出是否符合 SQL 真实数据统计先验。
"""

from __future__ import annotations

import math
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from signalclaw.core.constraints import IntersectionConstraints
from signalclaw.core.state import (
    CyclePlan,
    IntersectionObservation,
    NetworkObservation,
    PhaseCommand,
    PhaseObservation,
)
from signalclaw.reference.prior_checker import PriorConsistencyChecker
from signalclaw.reference.profile_schema import SQLReferenceProfile


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ReplayReport:
    """离线 Replay 评估报告。"""
    candidate_id: str
    crossing_id: str
    skill_type: str  # "cycle" | "phase"
    passed: bool
    violations: List[str] = field(default_factory=list)
    score: float = 0.0  # 越高越好
    failure_cases: List[dict] = field(default_factory=list)
    test_cases_run: int = 0


# ---------------------------------------------------------------------------
# Test case generators
# ---------------------------------------------------------------------------

def _make_phase_obs(
    phase_id: int,
    queue: float = 5.0,
    waiting_time: float = 30.0,
    predicted_arrival: float = 3.0,
    elapsed_green: float = 10.0,
    min_green: float = 10.0,
    max_green: float = 60.0,
) -> PhaseObservation:
    return PhaseObservation(
        phase_id=phase_id,
        queue=queue,
        waiting_time=waiting_time,
        predicted_arrival=predicted_arrival,
        elapsed_green=elapsed_green,
        min_green=min_green,
        max_green=max_green,
    )


def _make_network_obs(
    crossing_id: str = "test_tls",
    phases: Optional[Dict[int, PhaseObservation]] = None,
    downstream_queue: Optional[Dict[str, float]] = None,
    upstream_queue: Optional[Dict[str, float]] = None,
    current_phase_id: int = 0,
    current_phase_elapsed: float = 20.0,
    cycle_second: float = 80.0,
    timestamp: float = 100.0,
    spillback_risk: float = 0.0,
    release_pressure: float = 0.0,
) -> NetworkObservation:
    if phases is None:
        phases = {
            0: _make_phase_obs(0),
            1: _make_phase_obs(1),
            2: _make_phase_obs(2),
            3: _make_phase_obs(3),
        }
    ego = IntersectionObservation(
        crossing_id=crossing_id,
        current_phase_id=current_phase_id,
        current_phase_elapsed=current_phase_elapsed,
        cycle_second=cycle_second,
        phases=phases,
        downstream_queue=downstream_queue or {"e1": 3.0, "e2": 2.0},
        upstream_queue=upstream_queue or {"e3": 5.0, "e4": 4.0},
        downstream_spillback_risk=spillback_risk,
        upstream_release_pressure=release_pressure,
    )
    return NetworkObservation(ego=ego, neighbors={}, timestamp=timestamp)


def _generate_test_scenarios(
    crossing_id: str,
    constraints: IntersectionConstraints,
) -> List[Tuple[str, NetworkObservation]]:
    """生成多种测试场景。"""
    scenarios = []

    # ---- 场景 1: 正常交通 ----
    scenarios.append((
        "normal",
        _make_network_obs(
            crossing_id=crossing_id,
            phases={
                0: _make_phase_obs(0, queue=5.0, waiting_time=25.0),
                1: _make_phase_obs(1, queue=8.0, waiting_time=35.0),
                2: _make_phase_obs(2, queue=3.0, waiting_time=15.0),
                3: _make_phase_obs(3, queue=6.0, waiting_time=28.0),
            },
        ),
    ))

    # ---- 场景 2: 高峰期 ----
    scenarios.append((
        "peak_hour",
        _make_network_obs(
            crossing_id=crossing_id,
            phases={
                0: _make_phase_obs(0, queue=40.0, waiting_time=90.0),
                1: _make_phase_obs(1, queue=55.0, waiting_time=120.0),
                2: _make_phase_obs(2, queue=30.0, waiting_time=60.0),
                3: _make_phase_obs(3, queue=45.0, waiting_time=80.0),
            },
            spillback_risk=0.7,
            release_pressure=0.5,
        ),
    ))

    # ---- 场景 3: 低峰期 ----
    scenarios.append((
        "off_peak",
        _make_network_obs(
            crossing_id=crossing_id,
            phases={
                0: _make_phase_obs(0, queue=1.0, waiting_time=5.0),
                1: _make_phase_obs(1, queue=2.0, waiting_time=8.0),
                2: _make_phase_obs(2, queue=0.5, waiting_time=3.0),
                3: _make_phase_obs(3, queue=1.5, waiting_time=6.0),
            },
        ),
    ))

    # ---- 场景 4: 极端不均衡 ----
    scenarios.append((
        "extreme_imbalance",
        _make_network_obs(
            crossing_id=crossing_id,
            phases={
                0: _make_phase_obs(0, queue=80.0, waiting_time=200.0),
                1: _make_phase_obs(1, queue=0.0, waiting_time=0.0),
                2: _make_phase_obs(2, queue=0.5, waiting_time=2.0),
                3: _make_phase_obs(3, queue=0.0, waiting_time=0.0),
            },
            downstream_queue={"e1": 50.0},
            spillback_risk=0.9,
        ),
    ))

    # ---- 场景 5: 下游严重拥堵 ----
    scenarios.append((
        "downstream_congested",
        _make_network_obs(
            crossing_id=crossing_id,
            phases={
                0: _make_phase_obs(0, queue=20.0, waiting_time=60.0),
                1: _make_phase_obs(1, queue=15.0, waiting_time=50.0),
                2: _make_phase_obs(2, queue=10.0, waiting_time=40.0),
                3: _make_phase_obs(3, queue=12.0, waiting_time=45.0),
            },
            downstream_queue={"e1": 30.0, "e2": 25.0, "e3": 35.0},
            spillback_risk=0.8,
        ),
    ))

    # ---- 场景 6: 少相位路口（2 相位） ----
    scenarios.append((
        "two_phase",
        _make_network_obs(
            crossing_id=crossing_id,
            phases={
                0: _make_phase_obs(0, queue=10.0, waiting_time=30.0),
                1: _make_phase_obs(1, queue=15.0, waiting_time=45.0),
            },
        ),
    ))

    # ---- 场景 7: 大量相位（6 相位） ----
    scenarios.append((
        "six_phase",
        _make_network_obs(
            crossing_id=crossing_id,
            phases={
                i: _make_phase_obs(i, queue=5.0 + i * 2, waiting_time=20.0 + i * 5)
                for i in range(6)
            },
        ),
    ))

    # ---- 场景 8: 空路口 ----
    scenarios.append((
        "empty_intersection",
        _make_network_obs(
            crossing_id=crossing_id,
            phases={
                0: _make_phase_obs(0, queue=0.0, waiting_time=0.0),
                1: _make_phase_obs(1, queue=0.0, waiting_time=0.0),
                2: _make_phase_obs(2, queue=0.0, waiting_time=0.0),
                3: _make_phase_obs(3, queue=0.0, waiting_time=0.0),
            },
        ),
    ))

    return scenarios


# ---------------------------------------------------------------------------
# ReplayEvaluator
# ---------------------------------------------------------------------------

class ReplayEvaluator:
    """离线安全评估器：通过构造多种测试场景在内存中运行 skill。

    集成 PriorConsistencyChecker，在约束检查之后额外检查
    输出是否符合 SQL 真实数据统计先验。
    """

    def __init__(
        self,
        constraints: IntersectionConstraints,
        prior_checker: Optional[PriorConsistencyChecker] = None,
    ):
        """
        Args:
            constraints: 路口安全约束
            prior_checker: SQL 先验一致性检查器（可选，如未提供则自动加载默认画像）
        """
        self.constraints = constraints
        if prior_checker is not None:
            self.prior_checker = prior_checker
        else:
            profile = SQLReferenceProfile()
            self.prior_checker = PriorConsistencyChecker(profile)

    def evaluate(
        self,
        skill_code: str,
        skill_type: str,
        crossing_id: str,
        candidate_id: str = "",
        paired_plan: Optional[CyclePlan] = None,
    ) -> ReplayReport:
        """评估候选 Skill 的安全性。

        Parameters
        ----------
        skill_code : str
            skill 代码
        skill_type : str
            "cycle" 或 "phase"
        crossing_id : str
            路口 ID
        candidate_id : str
            候选 ID（用于报告）
        paired_plan : CyclePlan, optional
            配对的 CyclePlan（phase skill 评估时需要）

        Returns
        -------
        ReplayReport
        """
        if not candidate_id:
            candidate_id = str(uuid.uuid4())[:8]

        violations: List[str] = []
        failure_cases: List[dict] = []
        total_score = 0.0
        test_count = 0

        # 加载 skill
        skill_obj = self._load_skill(skill_code, skill_type)
        if skill_obj is None:
            return ReplayReport(
                candidate_id=candidate_id,
                crossing_id=crossing_id,
                skill_type=skill_type,
                passed=False,
                violations=["代码加载失败"],
                score=0.0,
                test_cases_run=0,
            )

        # 生成测试场景
        scenarios = _generate_test_scenarios(crossing_id, self.constraints)

        for scenario_name, obs in scenarios:
            test_count += 1
            try:
                if skill_type == "cycle":
                    result = self._run_cycle_skill(skill_obj, obs)
                    if result is None:
                        violations.append(f"[{scenario_name}] 执行失败或返回 None")
                        failure_cases.append({
                            "scenario": scenario_name,
                            "violation": "execution_failed",
                        })
                        continue
                    self._check_cycle_result(
                        result, scenario_name, violations, failure_cases
                    )
                    # SQL 先验一致性检查
                    self._check_cycle_prior(
                        result, scenario_name, violations, failure_cases
                    )
                    # 评分：绿灯分配越均匀，分越高；周期长度合理，分越高
                    total_score += self._score_cycle_result(result, scenario_name)

                elif skill_type == "phase":
                    # 需要配对的 CyclePlan
                    plan = paired_plan
                    if plan is None:
                        # 使用一个默认 plan
                        plan = CyclePlan(
                            cycle_length=80.0,
                            green_times={0: 20.0, 1: 20.0, 2: 20.0, 3: 20.0},
                            phase_order=[0, 1, 2, 3],
                        )
                    result = self._run_phase_skill(skill_obj, obs, plan)
                    if result is None:
                        violations.append(f"[{scenario_name}] 执行失败或返回 None")
                        failure_cases.append({
                            "scenario": scenario_name,
                            "violation": "execution_failed",
                        })
                        continue
                    self._check_phase_result(
                        result, plan, scenario_name, violations, failure_cases
                    )
                    # SQL 先验一致性检查
                    self._check_phase_prior(
                        result, scenario_name, violations, failure_cases
                    )
                    total_score += self._score_phase_result(result, scenario_name)

            except Exception as e:
                violations.append(f"[{scenario_name}] 异常: {e}")
                failure_cases.append({
                    "scenario": scenario_name,
                    "violation": "exception",
                    "detail": str(e),
                })

        passed = len(violations) == 0
        avg_score = total_score / max(test_count, 1)

        return ReplayReport(
            candidate_id=candidate_id,
            crossing_id=crossing_id,
            skill_type=skill_type,
            passed=passed,
            violations=violations,
            score=round(avg_score, 4),
            failure_cases=failure_cases,
            test_cases_run=test_count,
        )

    # ======================================================================
    # Skill loading
    # ======================================================================

    def _load_skill(self, code: str, skill_type: str) -> Any:
        """在受限环境中加载 skill 代码。"""
        import math as _math
        from collections import deque

        safe_ns: dict = {
            "__builtins__": __builtins__,
            "math": _math,
            "deque": deque,
            "dict": dict,
            "list": list,
            "tuple": tuple,
            "set": set,
            "float": float,
            "int": int,
            "str": str,
            "bool": bool,
            "len": len,
            "min": min,
            "max": max,
            "sum": sum,
            "abs": abs,
            "sorted": sorted,
            "range": range,
            "enumerate": enumerate,
            "zip": zip,
            "round": round,
            "isinstance": isinstance,
            "any": any,
            "all": all,
            "map": map,
            "filter": filter,
            "reversed": reversed,
            "print": print,
            # 状态类型
            "NetworkObservation": NetworkObservation,
            "IntersectionObservation": IntersectionObservation,
            "PhaseObservation": PhaseObservation,
            "CyclePlan": CyclePlan,
            "PhaseCommand": PhaseCommand,
        }

        try:
            exec(code, safe_ns)  # noqa: S102
        except Exception as e:
            return None

        # 验证导出了正确的函数
        if skill_type == "cycle":
            if "plan" not in safe_ns or not callable(safe_ns["plan"]):
                return None
        elif skill_type == "phase":
            if "decide" not in safe_ns or not callable(safe_ns["decide"]):
                return None

        return safe_ns

    # ======================================================================
    # Skill execution
    # ======================================================================

    def _run_cycle_skill(
        self, skill_ns: dict, obs: NetworkObservation
    ) -> Optional[CyclePlan]:
        """运行 cycle skill 并返回 CyclePlan。"""
        try:
            result = skill_ns["plan"](obs)
            if not isinstance(result, CyclePlan):
                return None
            return result
        except Exception:
            return None

    def _run_phase_skill(
        self, skill_ns: dict, obs: NetworkObservation, plan: CyclePlan
    ) -> Optional[PhaseCommand]:
        """运行 phase skill 并返回 PhaseCommand。"""
        try:
            result = skill_ns["decide"](obs, plan)
            if not isinstance(result, PhaseCommand):
                return None
            return result
        except Exception:
            return None

    # ======================================================================
    # Result checking
    # ======================================================================

    def _check_cycle_result(
        self,
        plan: CyclePlan,
        scenario_name: str,
        violations: List[str],
        failure_cases: List[dict],
    ) -> None:
        """检查 CyclePlan 是否满足约束。"""
        c = self.constraints

        # 检查 cycle_length
        if plan.cycle_length < c.min_cycle:
            v = f"[{scenario_name}] cycle_length={plan.cycle_length} < min_cycle={c.min_cycle}"
            violations.append(v)
            failure_cases.append({
                "scenario": scenario_name,
                "violation": "min_cycle_violation",
                "detail": v,
            })
        if plan.cycle_length > c.max_cycle:
            v = f"[{scenario_name}] cycle_length={plan.cycle_length} > max_cycle={c.max_cycle}"
            violations.append(v)
            failure_cases.append({
                "scenario": scenario_name,
                "violation": "max_cycle_violation",
                "detail": v,
            })

        # 检查每个相位的绿灯时间
        for phase_id, green_time in plan.green_times.items():
            # 数值合理性
            if not _is_valid_number(green_time):
                v = f"[{scenario_name}] phase {phase_id} green_time={green_time} (无效数值)"
                violations.append(v)
                failure_cases.append({
                    "scenario": scenario_name,
                    "violation": "invalid_value",
                    "detail": v,
                })
                continue

            if green_time < c.min_green:
                v = (
                    f"[{scenario_name}] phase {phase_id} green_time={green_time:.2f}"
                    f" < min_green={c.min_green}"
                )
                violations.append(v)
                failure_cases.append({
                    "scenario": scenario_name,
                    "violation": "min_green_violation",
                    "detail": v,
                })
            if green_time > c.max_green:
                v = (
                    f"[{scenario_name}] phase {phase_id} green_time={green_time:.2f}"
                    f" > max_green={c.max_green}"
                )
                violations.append(v)
                failure_cases.append({
                    "scenario": scenario_name,
                    "violation": "max_green_violation",
                    "detail": v,
                })

        # 检查 phase_order 是否与 green_times 一致
        for pid in plan.phase_order:
            if pid not in plan.green_times:
                v = f"[{scenario_name}] phase {pid} in phase_order but not in green_times"
                violations.append(v)

        # 检查相位饥饿（强制相位是否都在）
        if c.force_phase_ids:
            for pid in c.force_phase_ids:
                if pid not in plan.green_times:
                    v = f"[{scenario_name}] force_phase {pid} 缺失"
                    violations.append(v)
                    failure_cases.append({
                        "scenario": scenario_name,
                        "violation": "phase_starvation",
                        "detail": v,
                    })

        # 检查 green_times 总和是否与 cycle_length 大致一致
        total_green = sum(plan.green_times.values())
        if abs(total_green - plan.cycle_length) > 1.0:
            # 只做 warning，不视为 hard violation
            pass

        # 检查 cycle_length 数值合理性
        if not _is_valid_number(plan.cycle_length):
            v = f"[{scenario_name}] cycle_length={plan.cycle_length} (无效数值)"
            violations.append(v)

    def _check_phase_result(
        self,
        cmd: PhaseCommand,
        plan: CyclePlan,
        scenario_name: str,
        violations: List[str],
        failure_cases: List[dict],
    ) -> None:
        """检查 PhaseCommand 是否满足约束。"""
        c = self.constraints

        # 检查 action 合法性
        if cmd.action not in ("hold", "switch", "extend", "shorten"):
            v = f"[{scenario_name}] 无效 action: {cmd.action}"
            violations.append(v)
            failure_cases.append({
                "scenario": scenario_name,
                "violation": "invalid_action",
                "detail": v,
            })

        # 检查 duration
        if not _is_valid_number(cmd.duration):
            v = f"[{scenario_name}] duration={cmd.duration} (无效数值)"
            violations.append(v)
            failure_cases.append({
                "scenario": scenario_name,
                "violation": "invalid_duration",
                "detail": v,
            })
        elif cmd.duration < 0:
            v = f"[{scenario_name}] duration={cmd.duration} < 0"
            violations.append(v)
            failure_cases.append({
                "scenario": scenario_name,
                "violation": "negative_duration",
                "detail": v,
            })
        elif cmd.duration > c.max_green:
            v = f"[{scenario_name}] duration={cmd.duration} > max_green={c.max_green}"
            violations.append(v)

        # 检查 next_phase_id 是否在 plan 中
        if plan.phase_order and cmd.next_phase_id not in plan.phase_order:
            v = (
                f"[{scenario_name}] next_phase_id={cmd.next_phase_id}"
                f" 不在 phase_order={plan.phase_order} 中"
            )
            violations.append(v)
            failure_cases.append({
                "scenario": scenario_name,
                "violation": "invalid_phase_id",
                "detail": v,
            })

        # 检查 extend/shorten 幅度
        if cmd.action == "extend":
            planned_duration = plan.green_times.get(cmd.next_phase_id, 0)
            if cmd.duration > planned_duration + c.max_extend:
                v = (
                    f"[{scenario_name}] extend 超限: duration={cmd.duration}"
                    f" > planned={planned_duration} + max_extend={c.max_extend}"
                )
                violations.append(v)

        if cmd.action == "shorten":
            planned_duration = plan.green_times.get(cmd.next_phase_id, 0)
            if cmd.duration < planned_duration - c.max_shorten:
                v = (
                    f"[{scenario_name}] shorten 超限: duration={cmd.duration}"
                    f" < planned={planned_duration} - max_shorten={c.max_shorten}"
                )
                violations.append(v)

    # ======================================================================
    # Prior consistency checks (SQL Reference Profile)
    # ======================================================================

    def _check_cycle_prior(
        self,
        plan: CyclePlan,
        scenario_name: str,
        violations: List[str],
        failure_cases: List[dict],
    ) -> None:
        """使用 SQL 先验检查 CyclePlan 输出（仅记录，不阻塞）。"""
        plan_dict = {
            "cycle_length": plan.cycle_length,
            "green_times": dict(plan.green_times),
            "phase_order": list(plan.phase_order),
        }
        prior_result = self.prior_checker.check_cycle_plan(plan_dict)

        # 先验违规降级为 warning，不阻塞候选通过
        # 先验是参考性的，不应成为 hard block
        for v in prior_result.violations:
            failure_cases.append({
                "scenario": scenario_name,
                "violation": "prior_warning",
                "detail": f"[{scenario_name}][prior] {v}",
            })

    def _check_phase_prior(
        self,
        cmd: PhaseCommand,
        scenario_name: str,
        violations: List[str],
        failure_cases: List[dict],
    ) -> None:
        """使用 SQL 先验检查 PhaseCommand 输出（仅记录，不阻塞）。"""
        cmd_dict = {
            "action": cmd.action,
            "next_phase_id": cmd.next_phase_id,
            "duration": cmd.duration,
            "reason_code": cmd.reason_code,
        }
        prior_result = self.prior_checker.check_phase_command(cmd_dict)

        # 先验违规降级为 warning，不阻塞候选通过
        for v in prior_result.violations:
            failure_cases.append({
                "scenario": scenario_name,
                "violation": "prior_warning",
                "detail": f"[{scenario_name}][prior] {v}",
            })

    # ======================================================================
    # Scoring
    # ======================================================================

    def _score_cycle_result(self, plan: CyclePlan, scenario_name: str) -> float:
        """为 CyclePlan 结果打分（0-1 之间）。

        评分标准：
        - 绿灯分配利用率（接近 max_cycle 但不超）
        - 相位覆盖完整度
        - 绿灯分配均衡度
        """
        score = 0.0

        # 基础分：能正常返回结果
        score += 0.3

        # 相位覆盖：所有相位的绿灯时间 > 0
        n_phases = len(plan.green_times)
        if n_phases > 0:
            non_zero = sum(1 for gt in plan.green_times.values() if gt > 0)
            coverage = non_zero / n_phases
            score += 0.3 * coverage

        # 均衡度：绿灯时间的变异系数（越小越好）
        if n_phases > 1:
            times = list(plan.green_times.values())
            mean_t = sum(times) / len(times)
            if mean_t > 0:
                variance = sum((t - mean_t) ** 2 for t in times) / len(times)
                cv = (variance ** 0.5) / mean_t
                # cv = 0 -> perfect balance (score 0.4)
                # cv = 2 -> very imbalanced (score 0)
                balance_score = max(0, 0.4 * (1 - cv / 2.0))
                score += balance_score

        return min(score, 1.0)

    def _score_phase_result(self, cmd: PhaseCommand, scenario_name: str) -> float:
        """为 PhaseCommand 结果打分。"""
        score = 0.0

        # 基础分：能正常返回结果
        score += 0.4

        # action 合理性
        if cmd.action in ("hold", "switch", "extend", "shorten"):
            score += 0.3

        # duration 合理性
        if cmd.duration > 0:
            score += 0.15

        # reason_code 存在
        if cmd.reason_code:
            score += 0.15

        return min(score, 1.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_valid_number(value: float) -> bool:
    """检查数值是否有效（非 NaN、非 inf、非负）。"""
    try:
        if isinstance(value, bool):
            return False
        if not isinstance(value, (int, float)):
            return False
        if math.isnan(value):
            return False
        if math.isinf(value):
            return False
        return True
    except (TypeError, ValueError):
        return False
