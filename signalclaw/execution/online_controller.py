"""OnlineController: 在线控制器 — 整合所有执行组件。

不调用 GLM，不探索，只执行 frozen skills。

执行流程：
1. 每个 sim step 检查每个路口
2. cycle boundary -> 调用 CycleSkill.plan() -> 安全裁剪 -> 设定周期
3. phase decision time -> 调用 PhaseSkill.decide() -> 安全裁剪 -> 执行微调
4. 每个 step 都减少剩余时间
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from signalclaw.core.state import (
    NetworkObservation, IntersectionObservation,
    CyclePlan, PhaseCommand,
)
from signalclaw.core.constraints import NetworkConstraints
from signalclaw.execution.cycle_manager import CycleManager
from signalclaw.execution.phase_manager import PhaseManager
from signalclaw.execution.safety_layer import SafetyLayer
from signalclaw.execution.stats import ControllerStats
from signalclaw.skills.cohort import SkillCohort
from signalclaw.network.neighbor_graph import NeighborGraph


class OnlineController:
    """在线控制器 — 不调用 GLM，不探索，只执行 frozen skills。"""

    def __init__(self, cohort: SkillCohort, neighbor_graph: NeighborGraph,
                 constraints: NetworkConstraints,
                 decision_interval: float = 5.0,
                 sim_step_length: float = 1.0,
                 switch_cooldown: float = 15.0,
                 min_green_hold: float = 10.0,
                 max_switches_per_cycle: int = 2):
        self.cohort = cohort
        self.neighbor_graph = neighbor_graph
        self.safety_layer = SafetyLayer(
            constraints,
            switch_cooldown=5.0,        # 5秒冷却（平衡频率控制和灵活性）
            min_green_hold=5.0,         # 5秒最小绿灯（防止闪烁）
            max_switches_per_cycle=6,   # 每个 cycle 最多6次 switch（含计划内推进）
        )
        self.cycle_manager = CycleManager()
        self.phase_manager = PhaseManager(decision_interval=decision_interval)
        self.decision_interval = decision_interval
        self.sim_step_length = sim_step_length
        self.audit_log: List[dict] = []
        self.stats = ControllerStats()

    # ------------------------------------------------------------------
    # 核心步进
    # ------------------------------------------------------------------

    def step(self, tls_id: str, sim_time: float,
             all_obs: Dict[str, IntersectionObservation]) -> Optional[PhaseCommand]:
        """每个模拟步调用，返回要执行的相位命令（如果有的话）。

        返回 None 表示本次 step 不需要做任何操作。
        """
        cmd: Optional[PhaseCommand] = None

        # 每步都减少剩余时间
        self.cycle_manager.tick(tls_id, self.sim_step_length)

        # 构建 NetworkObservation
        net_obs = self._build_network_obs(tls_id, sim_time, all_obs)

        # --- 1. 检查周期边界 ---
        if self.cycle_manager.is_cycle_boundary(tls_id, sim_time):
            cmd = self._on_cycle_boundary(tls_id, sim_time, net_obs)
            return cmd

        # --- 2. 检查相位决策时刻 ---
        if self.phase_manager.should_decide(tls_id, self.cycle_manager, sim_time):
            cmd = self._on_phase_decision(tls_id, sim_time, net_obs)
            return cmd

        # --- 3. 检查是否需要自动推进相位 ---
        remaining = self.cycle_manager.get_remaining_green(tls_id)
        if remaining <= 0:
            cmd = self._on_phase_exhausted(tls_id, sim_time, net_obs)
            return cmd

        return None

    # ------------------------------------------------------------------
    # 周期边界处理
    # ------------------------------------------------------------------

    def _on_cycle_boundary(self, tls_id: str, sim_time: float,
                           net_obs: NetworkObservation) -> PhaseCommand:
        """周期边界：调用 CycleSkill 生成新计划，然后启动第一个相位。

        新周期开始时同步 PhaseSkill 内部状态，防止 PhaseSkill 在第一次
        decide() 时因 hash 不匹配而触发重复的 new_plan switch。
        """
        # 调用 cycle skill
        cycle_skill = self.cohort.get_cycle_skill(tls_id)
        raw_plan = cycle_skill.plan(net_obs)

        # 安全裁剪
        plan = self.safety_layer.clip_cycle_plan(raw_plan, tls_id)
        if plan != raw_plan:
            self.stats.safety_clip_count += 1

        # 设定计划
        self.cycle_manager.set_plan(tls_id, plan, sim_time)

        # --- 同步 PhaseSkill 内部状态到新周期 ---
        # CycleManager.set_plan 已重置 _phase_index=0, _phase_remaining=第一个相位时长
        # 必须让 PhaseSkill 也同步到这个状态，否则 PhaseSkill 在下一次
        # decide() 时会看到 hash 变化并触发 new_plan switch
        phase_skill = self.cohort.get_phase_skill(tls_id)
        self._sync_phase_skill_state(tls_id, phase_skill, plan)

        # 通知 SafetyLayer 新周期开始
        self.safety_layer.notify_cycle_start(tls_id, sim_time)

        # 获取第一个相位
        first_phase = plan.phase_order[0] if plan.phase_order else net_obs.ego.current_phase_id
        first_duration = plan.green_times.get(first_phase, 15.0)

        self.stats.cycle_plan_count += 1

        # 重置 phase manager 的决策计时
        self.phase_manager.mark_decided(tls_id, sim_time)

        # cycle_start 的 switch 也需要通知 SafetyLayer
        self.safety_layer.notify_green_phase_start(tls_id, sim_time)

        cmd = PhaseCommand(
            action="switch",
            next_phase_id=first_phase,
            duration=first_duration,
            reason_code="cycle_start",
        )

        self._log(tls_id, sim_time, "cycle_plan", cmd, plan=plan)
        return cmd

    # ------------------------------------------------------------------
    # 相位微调决策
    # ------------------------------------------------------------------

    def _on_phase_decision(self, tls_id: str, sim_time: float,
                           net_obs: NetworkObservation) -> PhaseCommand:
        """相位微调时刻：调用 PhaseSkill.decide()。

        关键：调用 PhaseSkill 前，先同步 CycleManager 的真实状态到
        PhaseSkill 能感知的观察数据中，防止 PhaseSkill 因内部状态
        与 CycleManager 不同步而产生重复 switch。
        """
        plan = self.cycle_manager.get_plan(tls_id)
        if plan is None:
            return PhaseCommand(
                action="hold",
                next_phase_id=net_obs.ego.current_phase_id,
                duration=self.decision_interval,
                reason_code="no_plan",
            )

        # --- 状态同步：用 CycleManager 的真实状态构建同步的 observation ---
        synced_obs = self._sync_obs_to_cycle_manager(tls_id, net_obs, plan)

        # --- 状态同步：将 CycleManager 状态注入 PhaseSkill 内部 ---
        phase_skill = self.cohort.get_phase_skill(tls_id)
        self._sync_phase_skill_state(tls_id, phase_skill, plan)

        # 调用 phase skill（用同步后的 observation）
        raw_cmd = phase_skill.decide(synced_obs, plan)

        # 安全裁剪 — 传入 sim_time 用于 cooldown/min_green 检查
        cmd = self.safety_layer.clip_phase_command(raw_cmd, plan, tls_id, sim_time)
        if cmd != raw_cmd:
            self.stats.safety_clip_count += 1
            # 检查是否被转为 hold（即被约束拒绝）
            if raw_cmd.action in ("switch", "shorten") and cmd.action == "hold":
                self.stats.safety_reject_count += 1
                if "cooldown_block" in cmd.reason_code:
                    self.stats.switch_cooldown_reject_count += 1
                elif "min_green_hold_block" in cmd.reason_code:
                    self.stats.min_green_reject_count += 1
                elif "cycle_switch_limit_block" in cmd.reason_code:
                    self.stats.cycle_switch_limit_reject_count += 1

        # 更新内部状态
        self._apply_phase_command(tls_id, cmd, sim_time)

        self.phase_manager.mark_decided(tls_id, sim_time)
        self.stats.phase_command_count += 1

        self._log(tls_id, sim_time, "phase_decision", cmd, plan=plan)
        return cmd

    # ------------------------------------------------------------------
    # 相位时间耗尽
    # ------------------------------------------------------------------

    def _on_phase_exhausted(self, tls_id: str, sim_time: float,
                            net_obs: NetworkObservation) -> Optional[PhaseCommand]:
        """当前相位绿灯时间耗尽，尝试推进到下一个相位。

        推进后同步 PhaseSkill 内部状态，防止 PhaseSkill 在下一次
        decide() 调用时产生与 CycleManager 重复的 phase_end switch。
        """
        plan = self.cycle_manager.get_plan(tls_id)
        if plan is None:
            return None

        next_phase = self.cycle_manager.advance_phase(tls_id, sim_time)
        if next_phase is None:
            # 周期结束，下一步会触发 cycle boundary
            return None

        # --- 同步 PhaseSkill 内部状态到 CycleManager 的真实状态 ---
        phase_skill = self.cohort.get_phase_skill(tls_id)
        self._sync_phase_skill_state(tls_id, phase_skill, plan)

        duration = plan.green_times.get(next_phase, 15.0)

        # phase_exhausted 的 switch 也需要通过 SafetyLayer 检查
        raw_cmd = PhaseCommand(
            action="switch",
            next_phase_id=next_phase,
            duration=duration,
            reason_code="phase_exhausted",
        )

        cmd = self.safety_layer.clip_phase_command(raw_cmd, plan, tls_id, sim_time,
                                                    is_planned_advance=True)
        if cmd != raw_cmd:
            self.stats.safety_clip_count += 1
            if raw_cmd.action in ("switch", "shorten") and cmd.action == "hold":
                self.stats.safety_reject_count += 1
                if "cooldown_block" in cmd.reason_code:
                    self.stats.switch_cooldown_reject_count += 1
                elif "min_green_hold_block" in cmd.reason_code:
                    self.stats.min_green_reject_count += 1
                elif "cycle_switch_limit_block" in cmd.reason_code:
                    self.stats.cycle_switch_limit_reject_count += 1

        # 如果 switch 被允许，通知绿色相位开始
        if cmd.action == "switch":
            self.safety_layer.notify_green_phase_start(tls_id, sim_time)
            self.stats.phase_switch_count += 1

        self._log(tls_id, sim_time, "phase_exhausted", cmd, plan=plan)
        return cmd

    # ------------------------------------------------------------------
    # 状态同步辅助
    # ------------------------------------------------------------------

    def _sync_phase_skill_state(self, tls_id: str, phase_skill: Any,
                                plan: CyclePlan) -> None:
        """将 CycleManager 的真实状态同步到 PhaseSkill 的内部变量。

        PhaseSkill 通过模块级全局变量 _phase_index, _phase_remaining,
        _current_plan_hash 维护自己的相位状态视图。这些变量通过
        _dynamic_load 的 safe_ns 闭包被 decide() 函数引用。

        此方法通过计算与 CycleManager 一致的 hash 并调用 _reset()（如果
        可用），然后直接设置 safe_ns 中的变量来同步状态。
        如果 PhaseSkill 没有 _reset 或无法直接访问内部变量，
        则在 decide() 调用前通过 _sync_obs_to_cycle_manager 构建同步的
        observation 来间接防止重复 switch。
        """
        cm_idx = self.cycle_manager.get_phase_index(tls_id)
        cm_remaining = self.cycle_manager.get_remaining_green(tls_id)

        # 尝试通过 _reset() 重置 PhaseSkill 状态
        if hasattr(phase_skill, 'reset') and callable(phase_skill.reset):
            phase_skill.reset()

        # 尝试直接注入状态到 PhaseSkill 的闭包命名空间
        # _dynamic_load 创建的 wrapper 只暴露了 decide 函数，
        # 但 decide 函数引用的 safe_ns 字典中的全局变量可以通过
        # decide.__globals__ 访问
        if hasattr(phase_skill, 'decide'):
            decide_fn = phase_skill.decide
            if hasattr(decide_fn, '__globals__'):
                g = decide_fn.__globals__
                # 计算与 plan 一致的 hash，防止 new_plan 重复 switch
                plan_hash = hash((
                    plan.cycle_length,
                    tuple(sorted(plan.green_times.items())),
                    tuple(plan.phase_order),
                ))
                g['_phase_index'] = cm_idx
                g['_phase_remaining'] = cm_remaining
                g['_current_plan_hash'] = plan_hash

    def _sync_obs_to_cycle_manager(
        self, tls_id: str, net_obs: NetworkObservation, plan: CyclePlan,
    ) -> NetworkObservation:
        """构建与 CycleManager 真实状态同步的 NetworkObservation。

        确保 observation 中的 current_phase_id 和 current_phase_elapsed
        反映 CycleManager 的真实相位状态，而非 SUMO 反馈的滞后状态。
        """
        cm_phase_id = self.cycle_manager.get_current_phase_id(tls_id)
        ego = net_obs.ego

        # 如果 CycleManager 知道当前相位（即有活跃计划），使用它的
        if cm_phase_id is not None:
            phase_start = self.cycle_manager._phase_start.get(tls_id, 0.0)
            # 计算从相位开始到现在经过的时间
            cm_elapsed = net_obs.timestamp - phase_start if net_obs.timestamp >= phase_start else 0.0

            # 只有在确实不同时才创建新的 observation
            if cm_phase_id != ego.current_phase_id:
                ego = IntersectionObservation(
                    crossing_id=ego.crossing_id,
                    current_phase_id=cm_phase_id,
                    current_phase_elapsed=cm_elapsed,
                    cycle_second=ego.cycle_second,
                    phases=ego.phases,
                    downstream_queue=ego.downstream_queue,
                    upstream_queue=ego.upstream_queue,
                    downstream_spillback_risk=ego.downstream_spillback_risk,
                    upstream_release_pressure=ego.upstream_release_pressure,
                )

        return NetworkObservation(
            ego=ego,
            neighbors=net_obs.neighbors,
            timestamp=net_obs.timestamp,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _apply_phase_command(self, tls_id: str, cmd: PhaseCommand,
                             sim_time: float) -> None:
        """根据 PhaseCommand 更新内部状态。"""
        if cmd.action == "extend":
            remaining = self.cycle_manager.get_remaining_green(tls_id)
            # extend 意味着在当前 remaining 基础上增加
            current_phase = self.cycle_manager.get_current_phase_id(tls_id)
            if current_phase is not None:
                plan = self.cycle_manager.get_plan(tls_id)
                base = plan.green_times.get(current_phase, 15.0) if plan else 15.0
                # cmd.duration 是总持续时间（含 extend）
                self.cycle_manager.set_remaining_green(tls_id, cmd.duration)
            self.stats.phase_extend_count += 1

        elif cmd.action == "shorten":
            self.cycle_manager.set_remaining_green(tls_id, cmd.duration)
            self.stats.phase_shorten_count += 1

        elif cmd.action == "switch":
            # switch 必须同步 CycleManager 的 phase_index 到目标相位
            plan = self.cycle_manager.get_plan(tls_id)
            if plan and plan.phase_order and cmd.next_phase_id in plan.phase_order:
                new_idx = plan.phase_order.index(cmd.next_phase_id)
                self.cycle_manager._phase_index[tls_id] = new_idx
                self.cycle_manager._phase_start[tls_id] = sim_time
            self.cycle_manager.set_remaining_green(tls_id, cmd.duration)
            self.stats.phase_switch_count += 1

        elif cmd.action == "hold":
            self.stats.phase_hold_count += 1

    def _build_network_obs(self, tls_id: str, sim_time: float,
                           all_obs: Dict[str, IntersectionObservation]
                           ) -> NetworkObservation:
        """构建 NetworkObservation，利用 neighbor graph 填充邻居观测。"""
        ego = all_obs.get(tls_id)
        if ego is None:
            # 不应该发生，做一个空的 fallback
            ego = IntersectionObservation(
                crossing_id=tls_id, current_phase_id=0,
                current_phase_elapsed=0.0, cycle_second=0.0,
                phases={}, downstream_queue={}, upstream_queue={},
            )

        # 获取一跳邻居的观测
        neighbor_ids = self.neighbor_graph.get_neighbor_tls_ids(tls_id)
        neighbors: Dict[str, IntersectionObservation] = {}
        for nid in neighbor_ids:
            if nid in all_obs:
                neighbors[nid] = all_obs[nid]

        return NetworkObservation(
            ego=ego,
            neighbors=neighbors,
            timestamp=sim_time,
        )

    def _log(self, tls_id: str, sim_time: float, event: str,
             cmd: PhaseCommand, plan: Optional[CyclePlan] = None) -> None:
        record = {
            "tls_id": tls_id,
            "sim_time": sim_time,
            "event": event,
            "action": cmd.action,
            "next_phase_id": cmd.next_phase_id,
            "duration": cmd.duration,
            "reason_code": cmd.reason_code,
        }
        if plan is not None:
            record["cycle_length"] = plan.cycle_length
        self.audit_log.append(record)

    # ------------------------------------------------------------------
    # 重置
    # ------------------------------------------------------------------

    def reset(self) -> None:
        self.cycle_manager.reset()
        self.phase_manager.reset()
        self.safety_layer.reset()
        self.audit_log.clear()
        self.stats = ControllerStats()
