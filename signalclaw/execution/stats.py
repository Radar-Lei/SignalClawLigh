"""ControllerStats: 在线控制器决策统计。"""

from dataclasses import dataclass, field


@dataclass
class ControllerStats:
    """在线控制器运行期间的各类决策计数。"""

    cycle_plan_count: int = 0
    phase_command_count: int = 0
    phase_extend_count: int = 0
    phase_shorten_count: int = 0
    phase_switch_count: int = 0
    phase_hold_count: int = 0
    safety_clip_count: int = 0
    safety_reject_count: int = 0

    # 新增: switch 约束拒绝统计
    switch_cooldown_reject_count: int = 0
    min_green_reject_count: int = 0
    cycle_switch_limit_reject_count: int = 0

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)
