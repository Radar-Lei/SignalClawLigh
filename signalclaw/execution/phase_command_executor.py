"""PhaseCommandExecutor: 统一 PhaseCommand 的执行逻辑。

将 runner.py 和 evaluator_sumo.py 中对 PhaseCommand 的执行统一到此处，
确保 switch 命令正确处理 yellow/all-red 过渡（不跳过中间状态）。

两种使用模式：
1. Runner 模式（直接使用 traci 连接）：
   executor = PhaseCommandExecutor.for_traci(traci, tls_data, constraints)
2. Evaluator 模式（使用 SumoTraCIAdapter）：
   executor = PhaseCommandExecutor.for_adapter(adapter, constraints)

核心方法：
- apply(command, tls_id): 将 PhaseCommand 映射为 SUMO TraCI 操作
- process_pending_switches(tls_ids): 每步调用，处理过渡完成后的 duration 设置
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple, Protocol, Any, List

from signalclaw.core.state import PhaseCommand
from signalclaw.core.constraints import NetworkConstraints


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def is_green_phase(state_str: str) -> bool:
    """检查 phase state 字符串是否表示绿灯相位（有 g/G 但没有 y）。"""
    has_green = any(c in 'gG' for c in state_str)
    has_yellow = any(c in 'y' for c in state_str)
    return has_green and not has_yellow


# ---------------------------------------------------------------------------
# Connection protocol — 屏蔽 traci / adapter 的接口差异
# ---------------------------------------------------------------------------

class ConnectionLike(Protocol):
    """PhaseCommandExecutor 所需的最小连接接口。

    traci 模块和 SumoTraCIAdapter 都满足此协议。
    """

    def get_current_phase(self, tls_id: str) -> int: ...
    def get_phase_state(self, tls_id: str, phase_index: int) -> str: ...
    def get_next_switch(self, tls_id: str) -> float: ...
    def get_sim_time(self) -> float: ...
    def get_num_phases(self, tls_id: str) -> int: ...
    def get_default_duration(self, tls_id: str, phase_index: int) -> float: ...
    def set_phase_duration(self, tls_id: str, duration: float) -> None: ...


class _TraCIConnection:
    """包装直接使用 traci 模块的连接（runner 模式）。"""

    def __init__(self, traci, tls_data: dict):
        self._traci = traci
        self._tls_data = tls_data

    def get_current_phase(self, tls_id: str) -> int:
        return self._traci.trafficlight.getPhase(tls_id)

    def get_phase_state(self, tls_id: str, phase_index: int) -> str:
        return self._tls_data[tls_id]['phases'][phase_index].state

    def get_next_switch(self, tls_id: str) -> float:
        return self._traci.trafficlight.getNextSwitch(tls_id)

    def get_sim_time(self) -> float:
        return self._traci.simulation.getTime()

    def get_num_phases(self, tls_id: str) -> int:
        return self._tls_data[tls_id]['num_phases']

    def get_default_duration(self, tls_id: str, phase_index: int) -> float:
        td = self._tls_data[tls_id]
        if phase_index < len(td['default_durations']):
            return td['default_durations'][phase_index]
        return 30.0

    def set_phase_duration(self, tls_id: str, duration: float) -> None:
        self._traci.trafficlight.setPhaseDuration(tls_id, duration)


class _AdapterConnection:
    """包装 SumoTraCIAdapter（evaluator 模式）。"""

    def __init__(self, adapter):
        self._adapter = adapter

    def get_current_phase(self, tls_id: str) -> int:
        return self._adapter._conn.tls.getPhase(tls_id)

    def get_phase_state(self, tls_id: str, phase_index: int) -> str:
        info = self._adapter.get_tls_info(tls_id)
        return info['phase_states'].get(phase_index, '')

    def get_next_switch(self, tls_id: str) -> float:
        return self._adapter._conn.tls.getNextSwitch(tls_id)

    def get_sim_time(self) -> float:
        return self._adapter.get_sim_time()

    def get_num_phases(self, tls_id: str) -> int:
        info = self._adapter.get_tls_info(tls_id)
        return info['num_phases']

    def get_default_duration(self, tls_id: str, phase_index: int) -> float:
        info = self._adapter.get_tls_info(tls_id)
        return info['phase_durations'].get(phase_index, 30.0)

    def set_phase_duration(self, tls_id: str, duration: float) -> None:
        self._adapter._conn.tls.setPhaseDuration(tls_id, duration)


# ---------------------------------------------------------------------------
# PhaseCommandExecutor
# ---------------------------------------------------------------------------

class PhaseCommandExecutor:
    """统一 PhaseCommand 执行逻辑。

    封装 hold/extend/shorten/switch 四种命令的 SUMO 操作映射，
    维护 per-tls 的 pending switch 状态，确保 switch 命令正确处理
    yellow/all-red 过渡。

    使用方式：
        # Runner 模式
        executor = PhaseCommandExecutor.for_traci(traci, tls_data, constraints)

        # Evaluator 模式
        executor = PhaseCommandExecutor.for_adapter(adapter, constraints)

        # 在仿真主循环中
        executor.process_pending_switches(tls_ids)
        cmd = controller.step(tls_id, sim_time, all_obs)
        if cmd is not None:
            executor.apply(cmd, tls_id)
    """

    def __init__(self, connection: ConnectionLike,
                 constraints: Optional[NetworkConstraints] = None):
        self._conn = connection
        self._constraints = constraints
        # switch 命令的 pending duration: tls_id -> (target_green_phase, duration)
        self._pending_switch_durations: Dict[str, Tuple[int, float]] = {}

    @classmethod
    def for_traci(cls, traci, tls_data: dict,
                  constraints: Optional[NetworkConstraints] = None) -> "PhaseCommandExecutor":
        """创建使用直接 traci 连接的 executor（runner 模式）。"""
        connection = _TraCIConnection(traci, tls_data)
        return cls(connection, constraints)

    @classmethod
    def for_adapter(cls, adapter,
                    constraints: Optional[NetworkConstraints] = None) -> "PhaseCommandExecutor":
        """创建使用 SumoTraCIAdapter 的 executor（evaluator 模式）。"""
        connection = _AdapterConnection(adapter)
        return cls(connection, constraints)

    # ------------------------------------------------------------------
    # Pending switch 处理
    # ------------------------------------------------------------------

    def process_pending_switches(self, tls_ids: List[str]) -> None:
        """每步调用，检查是否有过渡完成后需要设置 duration 的情况。

        当 SUMO 自然过渡到目标绿色相位时，设置之前记录的 duration。
        必须在每步 apply 命令之前调用。
        """
        for tls_id in tls_ids:
            pending = self._pending_switch_durations.get(tls_id)
            if pending is None:
                continue
            target_phase, target_duration = pending
            current_phase = self._conn.get_current_phase(tls_id)
            if current_phase == target_phase:
                state_str = self._conn.get_phase_state(tls_id, current_phase)
                if is_green_phase(state_str):
                    self._conn.set_phase_duration(tls_id, target_duration)
                    del self._pending_switch_durations[tls_id]

    # ------------------------------------------------------------------
    # 核心执行方法
    # ------------------------------------------------------------------

    def apply(self, cmd: PhaseCommand, tls_id: str) -> None:
        """将 PhaseCommand 映射为具体的 SUMO 操作。

        PhaseCommand.action:
        - hold:    保持当前相位，不做 TraCI 调用（让 SUMO 继续倒计时）
        - extend:  延长当前绿色相位 duration
        - shorten: 缩短当前绿色相位 duration（不低于 min_green）
        - switch:  切到 next_phase_id，处理 yellow/all-red 过渡
        """
        if cmd.action == "hold":
            # hold: 保持当前相位，不干预 SUMO 倒计时
            pass

        elif cmd.action == "extend":
            self._apply_extend(tls_id, cmd)

        elif cmd.action == "shorten":
            self._apply_shorten(tls_id, cmd)

        elif cmd.action == "switch":
            self._apply_switch(tls_id, cmd)

    # ------------------------------------------------------------------
    # 具体命令处理
    # ------------------------------------------------------------------

    def _apply_extend(self, tls_id: str, cmd: PhaseCommand) -> None:
        """延长当前绿色相位持续时间。"""
        current_phase = self._conn.get_current_phase(tls_id)
        state_str = self._conn.get_phase_state(tls_id, current_phase)
        if not is_green_phase(state_str):
            return

        remaining = self._conn.get_next_switch(tls_id) - self._conn.get_sim_time()
        default_dur = self._conn.get_default_duration(tls_id, current_phase)
        elapsed = max(0.0, default_dur - remaining)
        new_remaining = cmd.duration - elapsed
        if new_remaining > remaining and new_remaining > 0:
            self._conn.set_phase_duration(tls_id, new_remaining)

    def _apply_shorten(self, tls_id: str, cmd: PhaseCommand) -> None:
        """缩短当前绿色相位持续时间。"""
        current_phase = self._conn.get_current_phase(tls_id)
        state_str = self._conn.get_phase_state(tls_id, current_phase)
        if not is_green_phase(state_str):
            return

        remaining = self._conn.get_next_switch(tls_id) - self._conn.get_sim_time()
        default_dur = self._conn.get_default_duration(tls_id, current_phase)
        elapsed = max(0.0, default_dur - remaining)

        # 缩短后不能低于 min_green
        min_remaining = 0.0
        if self._constraints is not None:
            c = self._constraints.get(tls_id)
            min_remaining = max(0.0, c.min_green - elapsed)
        new_remaining = max(min_remaining, cmd.duration)
        if new_remaining < remaining:
            self._conn.set_phase_duration(tls_id, new_remaining)

    def _apply_switch(self, tls_id: str, cmd: PhaseCommand) -> None:
        """切换到目标绿色相位，正确处理 yellow/all-red 过渡。

        SUMO TLS 程序定义了完整的相位序列（green -> yellow -> all_red -> green）。
        不能跳过中间的 yellow/all-red 过渡。

        策略：
        1. 当前是绿色相位且目标是另一个绿色相位 -> 结束当前绿色相位
           （setPhaseDuration=1 让 SUMO 自然过渡 yellow/all-red 到目标）
        2. 当前已经是目标绿色相位 -> 设置新的 duration
        3. 当前在非绿色相位（yellow/all-red）-> 不干预过渡过程
        """
        target_green = cmd.next_phase_id
        num_phases = self._conn.get_num_phases(tls_id)

        # 确认目标相位在 TLS 程序中存在
        if target_green < 0 or target_green >= num_phases:
            return

        current_phase = self._conn.get_current_phase(tls_id)
        state_str = self._conn.get_phase_state(tls_id, current_phase)
        current_is_green = is_green_phase(state_str)

        if current_is_green:
            if current_phase == target_green:
                # 已经在目标绿色相位，设置新的 duration
                self._conn.set_phase_duration(tls_id, cmd.duration)
            else:
                # 需要切到另一个绿色相位
                # 结束当前绿色相位，让 SUMO 自然过渡 yellow -> all_red -> target green
                # 设置最小剩余时间（1 步），让 SUMO 尽快进入过渡
                remaining = self._conn.get_next_switch(tls_id) - self._conn.get_sim_time()
                if remaining > 1.0:
                    self._conn.set_phase_duration(tls_id, 1.0)
                # 记录目标绿色相位和期望的 duration，等过渡完成后设置
                self._pending_switch_durations[tls_id] = (target_green, cmd.duration)
        else:
            # 当前在非绿色相位（yellow/all-red 过渡中）
            # 不干预过渡过程，让 SUMO 自然完成过渡
            # pending switch duration 将在 process_pending_switches 中被检测和处理
            pass

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """清空所有内部状态。"""
        self._pending_switch_durations.clear()

    def has_pending_switch(self, tls_id: str) -> bool:
        """检查指定路口是否有 pending switch。"""
        return tls_id in self._pending_switch_durations
