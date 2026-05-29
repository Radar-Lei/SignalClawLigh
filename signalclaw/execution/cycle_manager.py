"""CycleManager: 周期边界检测与周期/相位状态跟踪。"""

from __future__ import annotations

from typing import Dict, Optional

from signalclaw.core.state import CyclePlan


class CycleManager:
    """管理每个路口的周期边界检测、相位推进和剩余时间跟踪。"""

    def __init__(self):
        self._plans: Dict[str, CyclePlan] = {}        # tls_id -> current cycle plan
        self._cycle_start: Dict[str, float] = {}      # tls_id -> cycle start sim_time
        self._phase_start: Dict[str, float] = {}      # tls_id -> current phase start time
        self._phase_index: Dict[str, int] = {}        # tls_id -> current phase index in plan.phase_order
        self._phase_remaining: Dict[str, float] = {}  # tls_id -> remaining green time

    # ------------------------------------------------------------------
    # 周期边界
    # ------------------------------------------------------------------

    def is_cycle_boundary(self, tls_id: str, sim_time: float) -> bool:
        """判断当前是否是周期边界。

        周期边界 = 第一次对该路口调用（还没有 plan），
        或者当前周期内所有相位都已执行完毕。
        """
        if tls_id not in self._plans:
            return True

        plan = self._plans[tls_id]
        if not plan.phase_order:
            return True

        idx = self._phase_index.get(tls_id, 0)
        remaining = self._phase_remaining.get(tls_id, 0.0)

        # 所有相位都执行完 + 剩余时间耗尽
        if idx >= len(plan.phase_order) - 1 and remaining <= 0:
            return True

        return False

    # ------------------------------------------------------------------
    # 周期计划管理
    # ------------------------------------------------------------------

    def set_plan(self, tls_id: str, plan: CyclePlan, sim_time: float) -> None:
        """设置新的周期计划，并重置相位状态到第一个相位。"""
        self._plans[tls_id] = plan
        self._cycle_start[tls_id] = sim_time
        if plan.phase_order:
            first_phase = plan.phase_order[0]
            self._phase_index[tls_id] = 0
            self._phase_start[tls_id] = sim_time
            self._phase_remaining[tls_id] = plan.green_times.get(first_phase, 15.0)
        else:
            self._phase_index[tls_id] = 0
            self._phase_start[tls_id] = sim_time
            self._phase_remaining[tls_id] = 0.0

    def get_plan(self, tls_id: str) -> Optional[CyclePlan]:
        return self._plans.get(tls_id)

    # ------------------------------------------------------------------
    # 相位推进
    # ------------------------------------------------------------------

    def is_phase_decision_time(self, tls_id: str, sim_time: float,
                               decision_interval: float = 5.0) -> bool:
        """判断是否到了相位微调决策时刻。

        只要该路口有活跃的 plan，且不在周期边界上，就可以做微调决策。
        实际的间隔控制由上层（PhaseManager 或主循环的步长）负责。
        """
        if tls_id not in self._plans:
            return False
        if self.is_cycle_boundary(tls_id, sim_time):
            return False
        return True

    def advance_phase(self, tls_id: str, sim_time: float) -> Optional[int]:
        """推进到下一个相位。

        返回新的 phase_id；如果已经到了最后一个相位（应触发新周期），返回 None。
        """
        plan = self._plans.get(tls_id)
        if plan is None or not plan.phase_order:
            return None

        idx = self._phase_index.get(tls_id, 0)
        next_idx = idx + 1

        if next_idx >= len(plan.phase_order):
            # 周期结束
            self._phase_remaining[tls_id] = 0.0
            return None

        next_phase = plan.phase_order[next_idx]
        self._phase_index[tls_id] = next_idx
        self._phase_start[tls_id] = sim_time
        self._phase_remaining[tls_id] = plan.green_times.get(next_phase, 15.0)
        return next_phase

    # ------------------------------------------------------------------
    # 剩余时间管理
    # ------------------------------------------------------------------

    def get_remaining_green(self, tls_id: str) -> float:
        return self._phase_remaining.get(tls_id, 0.0)

    def set_remaining_green(self, tls_id: str, remaining: float) -> None:
        self._phase_remaining[tls_id] = remaining

    def tick(self, tls_id: str, elapsed: float) -> None:
        """减少剩余绿灯时间。"""
        cur = self._phase_remaining.get(tls_id, 0.0)
        self._phase_remaining[tls_id] = max(0.0, cur - elapsed)

    def get_phase_index(self, tls_id: str) -> int:
        return self._phase_index.get(tls_id, 0)

    def get_current_phase_id(self, tls_id: str) -> Optional[int]:
        """获取当前在周期 plan 中正在执行的 phase_id。"""
        plan = self._plans.get(tls_id)
        if plan is None or not plan.phase_order:
            return None
        idx = self._phase_index.get(tls_id, 0)
        if idx >= len(plan.phase_order):
            return None
        return plan.phase_order[idx]

    # ------------------------------------------------------------------
    # 重置
    # ------------------------------------------------------------------

    def reset(self, tls_id: Optional[str] = None) -> None:
        if tls_id is not None:
            self._plans.pop(tls_id, None)
            self._cycle_start.pop(tls_id, None)
            self._phase_start.pop(tls_id, None)
            self._phase_index.pop(tls_id, None)
            self._phase_remaining.pop(tls_id, None)
        else:
            self._plans.clear()
            self._cycle_start.clear()
            self._phase_start.clear()
            self._phase_index.clear()
            self._phase_remaining.clear()
