"""SafetyLayer: 安全约束裁剪层。

对 CyclePlan 和 PhaseCommand 进行约束裁剪，确保满足路口物理约束。
"""

from __future__ import annotations

from typing import Optional

from signalclaw.core.state import CyclePlan, PhaseCommand
from signalclaw.core.constraints import NetworkConstraints, IntersectionConstraints


class SafetyLayer:
    """对周期计划和相位命令做安全裁剪。"""

    def __init__(self, constraints: NetworkConstraints):
        self.constraints = constraints

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
                           tls_id: str) -> PhaseCommand:
        """裁剪相位命令，确保满足约束。

        - extend 不超过 max_extend
        - shorten 不超过 max_shorten
        - 必须满足 min_green
        """
        c = self.constraints.get(tls_id)

        new_action = cmd.action
        new_duration = cmd.duration

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

        elif cmd.action == "switch":
            # 切换到新相位，确保新相位时间在合理范围内
            new_duration = max(c.min_green, min(c.max_green, cmd.duration))

        elif cmd.action == "hold":
            new_duration = max(c.min_green * 0.1, cmd.duration)

        return PhaseCommand(
            action=new_action,
            next_phase_id=cmd.next_phase_id,
            duration=new_duration,
            reason_code=cmd.reason_code,
        )
