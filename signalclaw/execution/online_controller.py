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
                 sim_step_length: float = 1.0):
        self.cohort = cohort
        self.neighbor_graph = neighbor_graph
        self.safety_layer = SafetyLayer(constraints)
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
        """周期边界：调用 CycleSkill 生成新计划，然后启动第一个相位。"""
        # 调用 cycle skill
        cycle_skill = self.cohort.get_cycle_skill(tls_id)
        raw_plan = cycle_skill.plan(net_obs)

        # 安全裁剪
        plan = self.safety_layer.clip_cycle_plan(raw_plan, tls_id)
        if plan != raw_plan:
            self.stats.safety_clip_count += 1

        # 设定计划
        self.cycle_manager.set_plan(tls_id, plan, sim_time)

        # 获取第一个相位
        first_phase = plan.phase_order[0] if plan.phase_order else net_obs.ego.current_phase_id
        first_duration = plan.green_times.get(first_phase, 15.0)

        self.stats.cycle_plan_count += 1

        # 重置 phase manager 的决策计时
        self.phase_manager.mark_decided(tls_id, sim_time)

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
        """相位微调时刻：调用 PhaseSkill.decide()。"""
        plan = self.cycle_manager.get_plan(tls_id)
        if plan is None:
            return PhaseCommand(
                action="hold",
                next_phase_id=net_obs.ego.current_phase_id,
                duration=self.decision_interval,
                reason_code="no_plan",
            )

        # 调用 phase skill
        phase_skill = self.cohort.get_phase_skill(tls_id)
        raw_cmd = phase_skill.decide(net_obs, plan)

        # 安全裁剪
        cmd = self.safety_layer.clip_phase_command(raw_cmd, plan, tls_id)
        if cmd != raw_cmd:
            self.stats.safety_clip_count += 1

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
        """当前相位绿灯时间耗尽，尝试推进到下一个相位。"""
        plan = self.cycle_manager.get_plan(tls_id)
        if plan is None:
            return None

        next_phase = self.cycle_manager.advance_phase(tls_id, sim_time)
        if next_phase is None:
            # 周期结束，下一步会触发 cycle boundary
            return None

        duration = plan.green_times.get(next_phase, 15.0)
        self.stats.phase_switch_count += 1

        cmd = PhaseCommand(
            action="switch",
            next_phase_id=next_phase,
            duration=duration,
            reason_code="phase_exhausted",
        )

        self._log(tls_id, sim_time, "phase_exhausted", cmd, plan=plan)
        return cmd

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
            # switch 在这里只更新 remaining，实际 phase index 由上层处理
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
        self.audit_log.clear()
        self.stats = ControllerStats()
