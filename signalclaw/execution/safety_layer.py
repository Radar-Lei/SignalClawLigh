"""SafetyLayer: 安全约束裁剪层。

对 CyclePlan 和 PhaseCommand 进行约束裁剪，确保满足路口物理约束。

包含:
- Switch cooldown: 一个相位切换后，至少 N 秒内不允许再次 switch
- Min green: 切到新绿色相位后，至少保持 M 秒
- Switch frequency limit: 每个 cycle 内最多允许 K 次切换
"""

from __future__ import annotations

from typing import Optional, Dict

from signalclaw.core.state import CyclePlan, PhaseCommand
from signalclaw.core.constraints import NetworkConstraints, IntersectionConstraints


class SafetyLayer:
    """对周期计划和相位命令做安全裁剪。

    新增约束:
    - switch_cooldown: 两次 switch 之间的最小间隔（秒）
    - min_green_hold: 新绿色相位必须持续的最小时间（秒）
    - max_switches_per_cycle: 每个 cycle 内允许的最大 switch 次数
    """

    def __init__(self, constraints: NetworkConstraints,
                 switch_cooldown: float = 15.0,
                 min_green_hold: float = 10.0,
                 max_switches_per_cycle: int = 2):
        self.constraints = constraints
        self.switch_cooldown = switch_cooldown
        self.min_green_hold = min_green_hold
        self.max_switches_per_cycle = max_switches_per_cycle

        # 跟踪每个 tls_id 的 switch 状态
        # tls_id -> 上次 switch 时的 sim_time
        self._last_switch_time: Dict[str, float] = {}
        # tls_id -> 当前绿色相位开始时的 sim_time
        self._green_phase_start: Dict[str, float] = {}
        # tls_id -> 当前 cycle 内的 switch 次数
        self._cycle_switch_count: Dict[str, int] = {}
        # tls_id -> 当前 cycle 的开始时间（用于检测 cycle 边界重置）
        self._cycle_start_time: Dict[str, float] = {}

        # 统计
        self.switch_cooldown_reject_count: int = 0
        self.min_green_reject_count: int = 0
        self.cycle_switch_limit_reject_count: int = 0

    def notify_cycle_start(self, tls_id: str, sim_time: float) -> None:
        """通知新周期开始，重置 cycle 内的 switch 计数。"""
        self._cycle_switch_count[tls_id] = 0
        self._cycle_start_time[tls_id] = sim_time

    def notify_green_phase_start(self, tls_id: str, sim_time: float) -> None:
        """通知新的绿色相位开始，记录开始时间。"""
        self._green_phase_start[tls_id] = sim_time

    def clip_cycle_plan(self, plan: CyclePlan, tls_id: str) -> CyclePlan:
        """裁剪周期计划，确保满足约束。

        - 每个相位的 green_time 在 [min_green, max_green] 之间
        - 总周期在 [min_cycle, max_cycle] 之间
        """
        c = self.constraints.get(tls_id)

        clipped_green: dict[int, float] = {}
        for phase_id, gt in plan.green_times.items():
            clipped_green[phase_id] = max(c.min_green, min(c.max_green, gt))

        # 如果总时间超限，按比例缩放
        total = sum(clipped_green.values())
        if total > c.max_cycle:
            scale = c.max_cycle / total
            clipped_green = {
                pid: max(c.min_green, gt * scale)
                for pid, gt in clipped_green.items()
            }
        elif total < c.min_cycle:
            # 如果总时间不足，均匀增加
            deficit = c.min_cycle - total
            n = max(len(clipped_green), 1)
            per_phase = deficit / n
            clipped_green = {pid: gt + per_phase for pid, gt in clipped_green.items()}

        new_cycle = sum(clipped_green.values())

        return CyclePlan(
            cycle_length=new_cycle,
            green_times=clipped_green,
            phase_order=plan.phase_order,
            offset_target=plan.offset_target,
        )

    def clip_phase_command(self, cmd: PhaseCommand, plan: CyclePlan,
                           tls_id: str, sim_time: float = 0.0,
                           is_planned_advance: bool = False) -> PhaseCommand:
        """裁剪相位命令，确保满足约束。

        包含:
        - extend 不超过 max_extend
        - shorten 不超过 max_shorten
        - 必须满足 min_green
        - switch 必须满足 cooldown / min_green_hold / cycle switch limit

        Args:
            is_planned_advance: 如果为 True，表示这是计划内的相位推进（如 phase_exhausted），
                免受 cooldown 和 cycle_switch_limit 约束，但仍受 min_green_hold 约束。
        """
        c = self.constraints.get(tls_id)

        new_action = cmd.action
        new_duration = cmd.duration
        new_reason = cmd.reason_code

        if cmd.action == "extend":
            # extend 的额外时间不超过 max_extend
            base_time = plan.green_times.get(cmd.next_phase_id, 0.0)
            max_allowed = base_time + c.max_extend
            new_duration = min(cmd.duration, max_allowed)
            # 确保不超过 max_green
            new_duration = min(new_duration, c.max_green)

        elif cmd.action == "shorten":
            # 缩短后不能低于 min_green
            new_duration = max(cmd.duration, c.min_green)
            # min_green_hold 检查: 如果绿色相位持续不足 min_green_hold，拒绝 shorten
            if sim_time > 0:
                green_start = self._green_phase_start.get(tls_id)
                if green_start is not None:
                    green_elapsed = sim_time - green_start
                else:
                    green_elapsed = self.min_green_hold  # 无记录，视为已满足
                if green_elapsed < self.min_green_hold:
                    # shorten 会缩短时间，在 min_green_hold 期间不允许
                    self.min_green_reject_count += 1
                    new_action = "hold"
                    new_duration = self.min_green_hold - green_elapsed
                    new_reason = f"min_green_hold_block|{cmd.reason_code}"

        elif cmd.action == "switch":
            if not is_planned_advance:
                # === Switch cooldown 检查 ===（计划内推进免检）
                last_switch = self._last_switch_time.get(tls_id, -self.switch_cooldown - 1.0)
                time_since_last_switch = sim_time - last_switch
                if time_since_last_switch < self.switch_cooldown:
                    self.switch_cooldown_reject_count += 1
                    # switch 被拒绝，转为 hold
                    return PhaseCommand(
                        action="hold",
                        next_phase_id=cmd.next_phase_id,
                        duration=self.switch_cooldown - time_since_last_switch,
                        reason_code=f"cooldown_block|{cmd.reason_code}",
                    )

                # === Cycle switch limit 检查 ===（计划内推进免检）
                cycle_switches = self._cycle_switch_count.get(tls_id, 0)
                if cycle_switches >= self.max_switches_per_cycle:
                    self.cycle_switch_limit_reject_count += 1
                    # switch 被拒绝，转为 hold
                    return PhaseCommand(
                        action="hold",
                        next_phase_id=cmd.next_phase_id,
                        duration=self.switch_cooldown,
                        reason_code=f"cycle_switch_limit_block|{cmd.reason_code}",
                    )

            # === Min green hold 检查 ===（计划内推进也需检查，但作为软约束仅记录不阻止）
            # 仅在已有绿色相位记录时才检查（跳过第一次 switch）
            green_start = self._green_phase_start.get(tls_id)
            if green_start is not None:
                green_elapsed = sim_time - green_start
            else:
                green_elapsed = self.min_green_hold  # 无记录，视为已满足
            if green_elapsed < self.min_green_hold and not is_planned_advance:
                self.min_green_reject_count += 1
                # switch 被拒绝，转为 hold
                return PhaseCommand(
                    action="hold",
                    next_phase_id=cmd.next_phase_id,
                    duration=self.min_green_hold - green_elapsed,
                    reason_code=f"min_green_hold_block|{cmd.reason_code}",
                )

            # switch 通过所有检查，记录状态
            self._last_switch_time[tls_id] = sim_time
            if not is_planned_advance:
                self._cycle_switch_count[tls_id] = cycle_switches + 1
            else:
                # 计划内推进不占用 cycle_switch_count，但仍然记录 last_switch_time
                pass
            # 新绿色相位从这里开始计时
            self._green_phase_start[tls_id] = sim_time

            # 切换到新相位，确保新相位时间在合理范围内
            new_duration = max(c.min_green, min(c.max_green, cmd.duration))

        elif cmd.action == "hold":
            new_duration = max(c.min_green * 0.1, cmd.duration)

        return PhaseCommand(
            action=new_action,
            next_phase_id=cmd.next_phase_id,
            duration=new_duration,
            reason_code=new_reason,
        )

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def get_reject_stats(self) -> dict:
        """获取约束拒绝统计。"""
        return {
            "switch_cooldown_reject_count": self.switch_cooldown_reject_count,
            "min_green_reject_count": self.min_green_reject_count,
            "cycle_switch_limit_reject_count": self.cycle_switch_limit_reject_count,
        }

    def reset(self) -> None:
        """重置所有跟踪状态。"""
        self._last_switch_time.clear()
        self._green_phase_start.clear()
        self._cycle_switch_count.clear()
        self._cycle_start_time.clear()
        self.switch_cooldown_reject_count = 0
        self.min_green_reject_count = 0
        self.cycle_switch_limit_reject_count = 0
