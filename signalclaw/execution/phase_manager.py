"""PhaseManager: 相位微调决策时机判断。"""

from __future__ import annotations

from signalclaw.execution.cycle_manager import CycleManager


class PhaseManager:
    """负责判断是否到了相位微调决策时刻。

    在每个 decision_interval 秒触发一次相位微调决策。
    """

    def __init__(self, decision_interval: float = 5.0):
        self.decision_interval = decision_interval
        self._last_decision: dict[str, float] = {}  # tls_id -> last decision sim_time

    def should_decide(self, tls_id: str, cycle_mgr: CycleManager,
                      sim_time: float) -> bool:
        """判断是否到了微调决策时刻。

        条件：
        1. 不在周期边界
        2. 距离上次决策已过 decision_interval 秒
        """
        if cycle_mgr.is_cycle_boundary(tls_id, sim_time):
            return False

        if tls_id not in cycle_mgr._plans:
            return False

        last = self._last_decision.get(tls_id, -self.decision_interval - 1.0)
        if sim_time - last >= self.decision_interval:
            return True

        return False

    def mark_decided(self, tls_id: str, sim_time: float) -> None:
        """标记已在 sim_time 做了决策。"""
        self._last_decision[tls_id] = sim_time

    def reset(self, tls_id: str = None) -> None:
        if tls_id is not None:
            self._last_decision.pop(tls_id, None)
        else:
            self._last_decision.clear()
