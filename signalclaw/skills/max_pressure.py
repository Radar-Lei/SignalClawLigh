"""Max Pressure baseline variants for traffic signal control.

Provides five variants:
- MaxPressureCyclicAllocation : original cyclic proportional allocation
- MaxPressureQueueOnly        : cyclic, incoming-queue-only (no downstream)
- MaxPressureCanonical         : classic pick-max-pressure with min_green constraint
- MaxPressureCyclicMovement   : cyclic with movement-level pressure
- MaxPressureSwitchLossAware  : canonical + hysteresis / cooldown / switch penalty

All variants implement plan(obs) -> CyclePlan and decide(obs, plan) -> PhaseCommand
so they are compatible with both CyclePlannerSkill and PhaseMicroSkill protocols.
"""

from __future__ import annotations

import abc
import math
from typing import Dict, List, Optional, Tuple

from signalclaw.core.state import (
    NetworkObservation,
    IntersectionObservation,
    PhaseObservation,
    CyclePlan,
    PhaseCommand,
)


# ======================================================================
# Abstract base
# ======================================================================

class _MaxPressureBase(abc.ABC):
    """Shared bookkeeping and helpers for all MaxPressure variants."""

    def __init__(
        self,
        min_green: float = 10.0,
        max_green: float = 60.0,
        cycle_length: float = 90.0,
        decision_interval: float = 5.0,
    ):
        self.min_green = min_green
        self.max_green = max_green
        self.default_cycle_length = cycle_length
        self.decision_interval = decision_interval
        # per-intersection bookkeeping
        self._phase_index: Dict[str, int] = {}
        self._phase_remaining: Dict[str, float] = {}
        self._current_plan: Dict[str, CyclePlan] = {}

    # ------------------------------------------------------------------
    # pressure computation — subclasses override
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        """Return the pressure score for *phase_id* at the given intersection."""

    # ------------------------------------------------------------------
    # plan / decide — subclasses may override for different strategies
    # ------------------------------------------------------------------

    def plan(self, obs: NetworkObservation) -> CyclePlan:
        """Default: compute pressure for every phase, then delegate allocation."""
        ego = obs.ego
        tls_id = ego.crossing_id
        green_phases = sorted(ego.phases.keys())

        if not green_phases:
            plan = CyclePlan(
                cycle_length=self.default_cycle_length,
                green_times={},
                phase_order=[],
            )
            self._current_plan[tls_id] = plan
            return plan

        pressures = {gp: self.compute_pressure(ego, gp) for gp in green_phases}
        green_times = self._allocate_green(green_phases, pressures)
        plan = CyclePlan(
            cycle_length=sum(green_times.values()),
            green_times=green_times,
            phase_order=green_phases,
        )
        self._current_plan[tls_id] = plan
        self._phase_index[tls_id] = 0
        self._phase_remaining[tls_id] = green_times.get(green_phases[0], self.min_green)
        return plan

    def decide(self, obs: NetworkObservation, plan: Optional[CyclePlan] = None) -> PhaseCommand:
        """Default cyclic decide: countdown remaining green, switch when exhausted."""
        ego = obs.ego
        tls_id = ego.crossing_id

        if plan is None:
            plan = self._current_plan.get(tls_id)
        if plan is None:
            return PhaseCommand(
                action="hold",
                next_phase_id=ego.current_phase_id,
                duration=self.decision_interval,
                reason_code="no_plan",
            )

        current_idx = self._phase_index.get(tls_id, 0)
        phase_order = plan.phase_order
        if current_idx >= len(phase_order):
            current_idx = 0
            self._phase_index[tls_id] = 0

        current_phase = phase_order[current_idx]
        remaining = self._phase_remaining.get(tls_id, 0)

        if remaining <= 0:
            next_idx = (current_idx + 1) % len(phase_order)
            next_phase = phase_order[next_idx]
            self._phase_index[tls_id] = next_idx
            self._phase_remaining[tls_id] = plan.green_times.get(next_phase, self.min_green)
            return PhaseCommand(
                action="switch",
                next_phase_id=next_phase,
                duration=self._phase_remaining[tls_id],
                reason_code="phase_exhausted",
            )

        self._phase_remaining[tls_id] = remaining - self.decision_interval
        return PhaseCommand(
            action="hold",
            next_phase_id=current_phase,
            duration=self.decision_interval,
            reason_code="continuing",
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _allocate_green(self, phases: List[int], pressures: Dict[int, float]) -> Dict[int, float]:
        """Proportional green-time allocation (shift to positive first)."""
        min_p = min(pressures.values())
        shifted = {gp: p - min_p + 1.0 for gp, p in pressures.items()}
        total_shifted = sum(shifted.values())

        green_times: Dict[int, float] = {}
        if total_shifted > 0:
            for gp in phases:
                gt = self.default_cycle_length * (shifted[gp] / total_shifted)
                green_times[gp] = max(self.min_green, min(self.max_green, gt))
        else:
            equal = self.default_cycle_length / len(phases)
            for gp in phases:
                green_times[gp] = max(self.min_green, min(self.max_green, equal))

        # cap total to 1.5x default cycle
        total = sum(green_times.values())
        max_cycle = self.default_cycle_length * 1.5
        if total > max_cycle:
            scale = max_cycle / total
            green_times = {
                gp: max(self.min_green, gt * scale) for gp, gt in green_times.items()
            }
        return green_times

    def reset(self, tls_id: Optional[str] = None) -> None:
        if tls_id:
            self._phase_index.pop(tls_id, None)
            self._phase_remaining.pop(tls_id, None)
            self._current_plan.pop(tls_id, None)
        else:
            self._phase_index.clear()
            self._phase_remaining.clear()
            self._current_plan.clear()


# ======================================================================
# Variant 1 — CyclicAllocation (original behaviour)
# ======================================================================

class MaxPressureCyclicAllocation(_MaxPressureBase):
    """Original implementation: cyclic fixed order, proportional green allocation.

    Downstream pressure is averaged over all downstream edges and then scaled by
    the number of phases.  Pressures are shifted to positive before proportional
    allocation.
    """

    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        phase_obs = obs.phases.get(phase_id)
        if phase_obs is None:
            return 0.0

        incoming_pressure = phase_obs.queue

        outgoing_pressure = sum(obs.downstream_queue.values()) if obs.downstream_queue else 0.0
        n_out = max(len(obs.downstream_queue), 1)
        outgoing_pressure = outgoing_pressure / n_out * len(obs.phases)

        return incoming_pressure - outgoing_pressure


# ======================================================================
# Variant 2 — QueueOnly (no downstream term)
# ======================================================================

class MaxPressureQueueOnly(_MaxPressureBase):
    """Cyclic, but pressure = incoming queue only (no downstream term).

    Useful as an ablation to measure whether the downstream computation
    adds signal or just noise.
    """

    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        phase_obs = obs.phases.get(phase_id)
        if phase_obs is None:
            return 0.0
        return phase_obs.queue


# ======================================================================
# Variant 3 — Canonical MaxPressure (pick-max-pressure per decision interval)
# ======================================================================

class MaxPressureCanonical(_MaxPressureBase):
    """Classic MaxPressure: every decision interval pick the phase with highest
    movement-specific pressure (upstream - downstream).

    * Min-green: the current phase must have been active for at least
      ``min_green`` seconds before a switch is allowed.
    * Pressure is computed per-movement: for each upstream edge that belongs to
      the given phase, we find the matching downstream edge(s) via a configurable
      mapping (``_phase_downstream_mapping``).  Without a mapping we fall back to
      a heuristic edge-name matching or a per-edge average.
    * plan() still returns a CyclePlan so it fits the skill_api protocol,
      but decide() is free to switch to any phase — it is not bound to
      the phase_order in the plan.

    Movement-level vs edge-average pressure
    ----------------------------------------
    True MaxPressure (Varaiya 2013) computes pressure as the sum over all
    *movements* (turning directions) served by a phase:

        pressure(phase) = sum_{m in movements(phase)} ( upstream(m) - downstream(m) )

    where upstream(m) is the queue on the incoming approach lane-group for
    movement m, and downstream(m) is the queue on the outgoing link that
    movement m feeds into.  This is **movement-level** because each phase's
    outgoing pressure only counts the downstream edges that are *actually
    reachable* from that phase's incoming edges.

    A simpler approximation (edge-average) takes the average of *all*
    downstream edges and uses the same value for every phase.  This loses
    phase-specific downstream information and may bias phases whose outgoing
    links happen to be congested.
    """

    # map to remember how long the current phase has been active
    _phase_elapsed: Dict[str, float]  # tls_id -> elapsed seconds

    # Optional: phase_id -> list of downstream edge names that this phase feeds into.
    # When provided, compute_pressure uses exact movement-level calculation.
    # When None, a heuristic edge-key matching is attempted first; if that
    # also fails, falls back to per-edge average with a TODO note.
    _phase_downstream_mapping: Optional[Dict[int, List[str]]]

    def __init__(
        self,
        phase_downstream_mapping: Optional[Dict[int, List[str]]] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._phase_elapsed: Dict[str, float] = {}
        self._phase_downstream_mapping = phase_downstream_mapping

    # ---- pressure ----

    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        """Movement-level pressure: upstream - downstream per movement edge.

        计算 logic 按优先级分为三级：

        1. **精确映射**：如果构造时提供了 ``_phase_downstream_mapping``,
           直接按 phase_id 取出对应的 downstream edge names, 只对这些边
           求和/求均值。这是真正的 movement-level 计算。

        2. **启发式匹配**：基于 upstream_queue 和 downstream_queue 的
           edge key 做节点名启发式匹配。SUMO 的 edge ID 通常编码了
           from_node → to_node 信息（如 ``e1_e2`` 或 ``e1toe2``）。
           对于 upstream edge ``A→B``, 尝试找到 downstream edge ``B→C``,
           即 downstream key 以 B（upstream 的 to_node）开头。

        3. **回退到全局平均**：以上两种方法都无法匹配时，对全部
           downstream_queue 取平均值。这不是 movement-specific 的，
           但总比忽略 downstream 信息要好。
        """
        phase_obs = obs.phases.get(phase_id)
        if phase_obs is None:
            return 0.0

        # Incoming: queue on the upstream edges belonging to this phase
        incoming = phase_obs.queue

        # --- Strategy 1: exact mapping ---
        if self._phase_downstream_mapping is not None:
            downstream_edges = self._phase_downstream_mapping.get(phase_id, [])
            if downstream_edges and obs.downstream_queue:
                # 真正的 movement-level: 只统计此 phase 对应的下游边
                matched = [
                    obs.downstream_queue[e]
                    for e in downstream_edges
                    if e in obs.downstream_queue
                ]
                if matched:
                    outgoing = sum(matched) / len(matched)
                else:
                    # mapping 中指定的边在观测中不存在（可能网络拓扑变化），回退
                    outgoing = self._fallback_downstream(obs)
            else:
                outgoing = self._fallback_downstream(obs)
            return incoming - outgoing

        # --- Strategy 2: heuristic edge-key matching ---
        heuristic_outgoing = self._heuristic_downstream(obs, phase_id)
        if heuristic_outgoing is not None:
            return incoming - heuristic_outgoing

        # --- Strategy 3: global average fallback ---
        outgoing = self._fallback_downstream(obs)
        return incoming - outgoing

    def _fallback_downstream(self, obs: IntersectionObservation) -> float:
        """全局平均 downstream（非 movement-specific，作为回退策略）。

        TODO: 此方法对所有 phase 使用相同的 downstream 平均值，
        不区分不同 phase 的 outgoing movements。如果有网络拓扑信息，
        应通过 ``phase_downstream_mapping`` 构造器参数提供精确映射，
        以实现真正的 movement-level 压力计算。
        """
        if obs.downstream_queue:
            return sum(obs.downstream_queue.values()) / len(obs.downstream_queue)
        return 0.0

    @staticmethod
    def _heuristic_downstream(
        obs: IntersectionObservation, phase_id: int,
    ) -> Optional[float]:
        """启发式匹配：从 upstream_queue 的 key 推断 downstream edge。

        尝试从 upstream edge key 中提取 to_node，然后在 downstream_queue
        中查找以该 to_node 为 from_node 的边。

        例如 upstream edge ``-e1toe2`` 或 ``e1_e2`` 的 to_node 是 ``e2``,
        则匹配 downstream edge 中以 ``e2`` 开头或包含 ``e2to`` / ``e2_`` 的 key。

        Returns None if heuristic matching fails (no matches found).
        """
        if not obs.upstream_queue or not obs.downstream_queue:
            return None

        # 收集 upstream edge keys 中可能与 phase_id 相关的 to_node 候选
        # 注意：upstream_queue 是整个交叉口的入口边，不是 per-phase 的，
        # 但 PhaseObservation.queue 已经是该 phase 对应的排队数。
        # 这里我们尝试匹配 upstream_queue 和 downstream_queue 的 key 关系。
        upstream_keys = list(obs.upstream_queue.keys())
        downstream_keys = list(obs.downstream_queue.keys())

        if not upstream_keys or not downstream_keys:
            return None

        matched_downstream_values: List[float] = []

        for up_key in upstream_keys:
            # 尝试从 upstream key 提取 to_node
            to_node = _extract_to_node(up_key)
            if to_node is None:
                continue
            # 在 downstream 中查找以 to_node 为 from_node 的边
            for dn_key in downstream_keys:
                dn_from = _extract_from_node(dn_key)
                if dn_from is not None and dn_from == to_node:
                    matched_downstream_values.append(obs.downstream_queue[dn_key])

        if matched_downstream_values:
            return sum(matched_downstream_values) / len(matched_downstream_values)

        return None

    # ---- plan ----

    def plan(self, obs: NetworkObservation) -> CyclePlan:
        """生成基于 movement-level 压力比例分配的周期计划。

        核心逻辑：
        1. 对每个相位使用 Canonical 的 movement-level 压力计算
        2. 按压力比例分配绿灯时间（复用 _allocate_green 的 shift+proportional 逻辑）
        3. 确保 min_green / max_green 约束
        4. 压力为 0 或负数的相位至少获得 min_green

        这样即使 runner.py 只调用 plan()（不调用 decide()），
        MaxPressureCanonical 也不会退化为简单等分，而是按压力比例分配。
        """
        ego = obs.ego
        tls_id = ego.crossing_id
        green_phases = sorted(ego.phases.keys())

        if not green_phases:
            plan = CyclePlan(
                cycle_length=self.default_cycle_length,
                green_times={},
                phase_order=[],
            )
            self._current_plan[tls_id] = plan
            return plan

        # 使用 Canonical 的 movement-level 压力计算
        pressures = {gp: self.compute_pressure(ego, gp) for gp in green_phases}

        # 按压力比例分配绿灯时间（复用基类的 _allocate_green）
        green_times = self._allocate_green(green_phases, pressures)

        plan = CyclePlan(
            cycle_length=sum(green_times.values()),
            green_times=green_times,
            phase_order=green_phases,
        )
        self._current_plan[tls_id] = plan
        self._phase_index[tls_id] = 0
        self._phase_remaining[tls_id] = green_times.get(green_phases[0], self.min_green)
        self._phase_elapsed[tls_id] = 0.0
        return plan

    # ---- decide ----

    def decide(self, obs: NetworkObservation, plan: Optional[CyclePlan] = None) -> PhaseCommand:
        ego = obs.ego
        tls_id = ego.crossing_id

        if plan is None:
            plan = self._current_plan.get(tls_id)
        if plan is None or not plan.phase_order:
            return PhaseCommand(
                action="hold",
                next_phase_id=ego.current_phase_id,
                duration=self.decision_interval,
                reason_code="no_plan",
            )

        elapsed = self._phase_elapsed.get(tls_id, 0.0) + self.decision_interval
        self._phase_elapsed[tls_id] = elapsed

        current_phase = ego.current_phase_id

        # If min_green not yet satisfied, must hold
        if elapsed < self.min_green:
            remaining = self._phase_remaining.get(tls_id, self.min_green)
            self._phase_remaining[tls_id] = remaining - self.decision_interval
            return PhaseCommand(
                action="hold",
                next_phase_id=current_phase,
                duration=self.decision_interval,
                reason_code="min_green_not_met",
            )

        # Compute pressure for every phase
        pressures = {gp: self.compute_pressure(ego, gp) for gp in plan.phase_order}
        best_phase = max(pressures, key=lambda gp: pressures[gp])
        best_pressure = pressures[best_phase]
        current_pressure = pressures.get(current_phase, -math.inf)

        if best_phase != current_phase and best_pressure > current_pressure:
            # Switch to the highest-pressure phase
            duration = max(self.min_green, min(self.max_green,
                                               self.default_cycle_length / len(plan.phase_order)))
            self._phase_index[tls_id] = plan.phase_order.index(best_phase)
            self._phase_remaining[tls_id] = duration
            self._phase_elapsed[tls_id] = 0.0
            return PhaseCommand(
                action="switch",
                next_phase_id=best_phase,
                duration=duration,
                reason_code="max_pressure_switch",
            )

        # Hold current phase
        remaining = self._phase_remaining.get(tls_id, self.min_green)
        self._phase_remaining[tls_id] = remaining - self.decision_interval
        return PhaseCommand(
            action="hold",
            next_phase_id=current_phase,
            duration=self.decision_interval,
            reason_code="continuing",
        )

    # ---- reset ----

    def reset(self, tls_id: Optional[str] = None) -> None:
        super().reset(tls_id)
        if tls_id:
            self._phase_elapsed.pop(tls_id, None)
        else:
            self._phase_elapsed.clear()


# ======================================================================
# Variant 4 — CyclicMovement (fixed order + movement-level pressure)
# ======================================================================

class MaxPressureCyclicMovement(_MaxPressureBase):
    """Fixed phase order (cyclic) with movement-level pressure.

    Like CyclicAllocation but pressure is computed per-movement
    (upstream edge - downstream edge), not via the global downstream
    average.  Green time is still allocated proportionally within the
    fixed cycle.
    """

    def compute_pressure(self, obs: IntersectionObservation, phase_id: int) -> float:
        phase_obs = obs.phases.get(phase_id)
        if phase_obs is None:
            return 0.0

        incoming = phase_obs.queue

        # Movement-specific downstream: average downstream queue per edge
        if obs.downstream_queue:
            outgoing = sum(obs.downstream_queue.values()) / len(obs.downstream_queue)
        else:
            outgoing = 0.0

        return incoming - outgoing

    # plan / decide inherited from _MaxPressureBase (cyclic proportional)


# ======================================================================
# Variant 5 — SwitchLossAware (canonical + switching cost awareness)
# ======================================================================

class MaxPressureSwitchLossAware(MaxPressureCanonical):
    """考虑切换损失的 MaxPressure 变体

    在 Canonical 变体的基础上引入以下约束：

    1. **min_green / max_green**: 最小 / 最大绿灯时间，防止频繁切换或单相位过长。
    2. **yellow + all-red loss**: 切换时考虑黄灯和全红时间的损失（通过
       ``switch_penalty`` 间接体现）。
    3. **cooldown**: 切换后的冷却期，冷却期内不进行下一次切换。
    4. **pressure hysteresis**: 新相位压力必须显著高于当前相位才触发切换，
       避免因压力微小波动导致无谓切换。
    5. **switch_penalty**: 切换惩罚项，将切换的时间损失折算为压力阈值增量。

    继承自 MaxPressureCanonical，复用其 movement-level 压力计算逻辑。
    """

    def __init__(
        self,
        decision_interval: float = 5.0,
        min_green: float = 10.0,
        max_green: float = 60.0,
        cycle_length: float = 90.0,
        yellow_time: float = 3.0,
        all_red_time: float = 2.0,
        cooldown_time: float = 5.0,
        hysteresis_ratio: float = 0.15,
        switch_penalty: float = 5.0,
        **kwargs,
    ):
        super().__init__(
            min_green=min_green,
            max_green=max_green,
            cycle_length=cycle_length,
            decision_interval=decision_interval,
            **kwargs,
        )
        self.yellow_time = yellow_time
        self.all_red_time = all_red_time
        self.cooldown_time = cooldown_time
        self.hysteresis_ratio = hysteresis_ratio
        self.switch_penalty = switch_penalty
        # Per-intersection cooldown state
        self._in_cooldown: Dict[str, bool] = {}
        self._cooldown_remaining: Dict[str, float] = {}

    # ---- helpers ----

    def _compute_phase_pressures(
        self, obs: IntersectionObservation, phase_order: List[int],
    ) -> Dict[int, float]:
        """Compute pressure for every candidate phase."""
        return {gp: self.compute_pressure(obs, gp) for gp in phase_order}

    # ---- plan (overrides Canonical) ----

    def plan(self, obs: NetworkObservation) -> CyclePlan:
        """带切换损失感知的周期计划生成。

        在 Canonical 按压力比例分配的基础上增加：
        1. cooldown 期间保持上一次分配不变（避免刚切换后又剧烈调整）
        2. hysteresis 判断：新分配与上次分配差异超过阈值时才改变
        """
        ego = obs.ego
        tls_id = ego.crossing_id
        green_phases = sorted(ego.phases.keys())

        if not green_phases:
            plan = CyclePlan(
                cycle_length=self.default_cycle_length,
                green_times={},
                phase_order=[],
            )
            self._current_plan[tls_id] = plan
            return plan

        # Guard: cooldown 期间保持上次分配
        in_cooldown = self._in_cooldown.get(tls_id, False)
        if in_cooldown:
            cd = self._cooldown_remaining.get(tls_id, 0.0) - self.decision_interval
            if cd > 0:
                self._cooldown_remaining[tls_id] = cd
                prev_plan = self._current_plan.get(tls_id)
                if prev_plan and prev_plan.phase_order:
                    self._phase_elapsed[tls_id] = (
                        self._phase_elapsed.get(tls_id, 0.0) + self.decision_interval
                    )
                    return prev_plan
                # 无前次计划，继续正常计算
            else:
                self._in_cooldown[tls_id] = False
                self._cooldown_remaining[tls_id] = 0.0

        # 计算压力并按比例分配（复用基类逻辑）
        pressures = self._compute_phase_pressures(ego, green_phases)
        new_green_times = self._allocate_green(green_phases, pressures)

        # Hysteresis: 如果有上一次计划，检查分配变化是否足够显著
        prev_plan = self._current_plan.get(tls_id)
        if prev_plan and prev_plan.phase_order == green_phases:
            max_change_ratio = 0.0
            for gp in green_phases:
                old_gt = prev_plan.green_times.get(gp, self.min_green)
                new_gt = new_green_times.get(gp, self.min_green)
                if old_gt > 0:
                    change = abs(new_gt - old_gt) / old_gt
                    max_change_ratio = max(max_change_ratio, change)

            # 变化不够显著，保持上次分配
            if max_change_ratio < self.hysteresis_ratio:
                self._phase_elapsed[tls_id] = (
                    self._phase_elapsed.get(tls_id, 0.0) + self.decision_interval
                )
                return prev_plan

        # 变化显著或首次计算：采用新分配
        plan = CyclePlan(
            cycle_length=sum(new_green_times.values()),
            green_times=new_green_times,
            phase_order=green_phases,
        )
        self._current_plan[tls_id] = plan
        self._phase_index[tls_id] = 0
        self._phase_remaining[tls_id] = new_green_times.get(
            green_phases[0], self.min_green
        )
        self._phase_elapsed[tls_id] = self._phase_elapsed.get(tls_id, 0.0)
        return plan

    # ---- decide (overrides Canonical) ----

    def decide(self, obs: NetworkObservation, plan: Optional[CyclePlan] = None) -> PhaseCommand:
        """考虑切换损失的决策。

        决策逻辑：
        1. min_green 未满足 → 保持当前相位。
        2. 冷却期内 → 保持当前相位。
        3. 计算所有相位压力，找到最佳候选。
        4. hysteresis 判据：候选压力 > 当前压力 * (1 + ratio) + penalty 才切换。
        5. max_green 到期时强制切换到最佳候选（仍需候选 ≠ 当前）。
        """
        ego = obs.ego
        tls_id = ego.crossing_id

        if plan is None:
            plan = self._current_plan.get(tls_id)
        if plan is None or not plan.phase_order:
            return PhaseCommand(
                action="hold",
                next_phase_id=ego.current_phase_id,
                duration=self.decision_interval,
                reason_code="no_plan",
            )

        # Advance elapsed time
        elapsed = self._phase_elapsed.get(tls_id, 0.0) + self.decision_interval
        self._phase_elapsed[tls_id] = elapsed

        current_phase = ego.current_phase_id

        # ---- Guard 1: min_green not yet satisfied ----
        if elapsed < self.min_green:
            remaining = self._phase_remaining.get(tls_id, self.min_green)
            self._phase_remaining[tls_id] = remaining - self.decision_interval
            return PhaseCommand(
                action="hold",
                next_phase_id=current_phase,
                duration=self.decision_interval,
                reason_code="min_green_not_met",
            )

        # ---- Guard 2: cooldown period ----
        in_cooldown = self._in_cooldown.get(tls_id, False)
        if in_cooldown:
            cd = self._cooldown_remaining.get(tls_id, 0.0) - self.decision_interval
            if cd > 0:
                self._cooldown_remaining[tls_id] = cd
                remaining = self._phase_remaining.get(tls_id, self.min_green)
                self._phase_remaining[tls_id] = remaining - self.decision_interval
                return PhaseCommand(
                    action="hold",
                    next_phase_id=current_phase,
                    duration=self.decision_interval,
                    reason_code="cooldown",
                )
            else:
                self._in_cooldown[tls_id] = False
                self._cooldown_remaining[tls_id] = 0.0

        # ---- Compute pressures ----
        pressures = self._compute_phase_pressures(ego, plan.phase_order)
        best_phase = max(pressures, key=lambda gp: pressures[gp])
        best_pressure = pressures[best_phase]
        current_pressure = pressures.get(current_phase, -math.inf)

        # ---- Hysteresis switch ----
        if best_phase != current_phase:
            # Total switch loss = yellow + all-red (informational; penalty already
            # captures the combined cost).
            switch_loss = self.yellow_time + self.all_red_time  # noqa: F841
            threshold = current_pressure * (1.0 + self.hysteresis_ratio) + self.switch_penalty
            if best_pressure > threshold:
                duration = max(
                    self.min_green,
                    min(self.max_green, self.default_cycle_length / len(plan.phase_order)),
                )
                self._phase_index[tls_id] = plan.phase_order.index(best_phase)
                self._phase_remaining[tls_id] = duration
                self._phase_elapsed[tls_id] = 0.0
                self._in_cooldown[tls_id] = True
                self._cooldown_remaining[tls_id] = self.cooldown_time
                return PhaseCommand(
                    action="switch",
                    next_phase_id=best_phase,
                    duration=duration,
                    reason_code="switch_loss_aware_switch",
                )

        # ---- max_green forced switch ----
        if elapsed >= self.max_green and best_phase != current_phase:
            duration = max(
                self.min_green,
                min(self.max_green, self.default_cycle_length / len(plan.phase_order)),
            )
            self._phase_index[tls_id] = plan.phase_order.index(best_phase)
            self._phase_remaining[tls_id] = duration
            self._phase_elapsed[tls_id] = 0.0
            self._in_cooldown[tls_id] = True
            self._cooldown_remaining[tls_id] = self.cooldown_time
            return PhaseCommand(
                action="switch",
                next_phase_id=best_phase,
                duration=duration,
                reason_code="max_green_forced_switch",
            )

        # ---- Hold ----
        remaining = self._phase_remaining.get(tls_id, self.min_green)
        self._phase_remaining[tls_id] = remaining - self.decision_interval
        return PhaseCommand(
            action="hold",
            next_phase_id=current_phase,
            duration=self.decision_interval,
            reason_code="continuing",
        )

    # ---- reset ----

    def reset(self, tls_id: Optional[str] = None) -> None:
        super().reset(tls_id)
        if tls_id:
            self._in_cooldown.pop(tls_id, None)
            self._cooldown_remaining.pop(tls_id, None)
        else:
            self._in_cooldown.clear()
            self._cooldown_remaining.clear()


# ======================================================================
# Backward-compatible alias
# ======================================================================

# NOTE (2025-05): 将默认从 MaxPressureCyclicAllocation 改为 MaxPressureCanonical。
# 原因：CyclicAllocation 使用全局下游队列平均，压力计算过于粗糙，
# 且按固定循环顺序执行——不是真正意义上的 MaxPressure 算法。
# Canonical 变体在每个 decision interval 自由选择压力最高的相位，
# 符合 Varaiya (2013) 原始论文的定义，是学术界通用的 baseline。
MaxPressureSkill = MaxPressureCanonical


# ======================================================================
# Edge-key heuristic helpers (SUMO-style edge naming)
# ======================================================================

def _extract_to_node(edge_key: str) -> Optional[str]:
    """从 SUMO 风格的 edge key 提取 to_node。

    支持的格式：
    - ``prefix_fromNode_toNode_suffix`` (含两个下划线分隔的节点)
    - ``fromNode_toNode`` (两个下划线分隔的节点)
    - ``-fromNode_toNode`` (SUMO 反向边前缀 ``-``)
    - ``fromNodeNtoNodeN`` (SUMO 默认 edge ID 格式，如 ``gneE0toE1``)

    返回第二个节点标识（to_node），如果没有匹配则返回 None。
    """
    # 去除 SUMO 反向边前缀
    clean = edge_key.lstrip("-")

    # 尝试下划线分隔: from_to
    parts = clean.split("_")
    if len(parts) >= 2:
        return parts[-1]

    # 尝试 "XtoY" 格式 (SUMO default junction edge naming)
    if "to" in clean:
        idx = clean.rfind("to")
        return clean[idx + 2:]

    return None


def _extract_from_node(edge_key: str) -> Optional[str]:
    """从 SUMO 风格的 edge key 提取 from_node。"""
    clean = edge_key.lstrip("-")

    parts = clean.split("_")
    if len(parts) >= 2:
        return parts[0]

    if "to" in clean:
        idx = clean.find("to")
        return clean[:idx]

    return None


# ======================================================================
# Factory
# ======================================================================

_VARIANTS = {
    "cyclic_allocation": MaxPressureCyclicAllocation,
    "queue_only": MaxPressureQueueOnly,
    "canonical": MaxPressureCanonical,
    "cyclic_movement": MaxPressureCyclicMovement,
    "switch_loss_aware": MaxPressureSwitchLossAware,
}


def create_max_pressure(variant: str, **kwargs) -> _MaxPressureBase:
    """Factory: return a MaxPressure variant instance.

    Parameters
    ----------
    variant : str
        One of "cyclic_allocation", "queue_only", "canonical", "cyclic_movement",
        "switch_loss_aware".
    **kwargs
        Forwarded to the variant constructor (min_green, max_green, cycle_length, …).
    """
    cls = _VARIANTS.get(variant)
    if cls is None:
        raise ValueError(
            f"Unknown MaxPressure variant '{variant}'. "
            f"Available: {list(_VARIANTS.keys())}"
        )
    return cls(**kwargs)
