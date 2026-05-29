from signalclaw.core.state import CyclePlan, PhaseCommand, NetworkObservation
from signalclaw.core.constraints import NetworkConstraints

class SafetyLayer:
    def __init__(self, constraints: NetworkConstraints):
        self.constraints = constraints

    def clip_cycle_plan(self, plan: CyclePlan, tls_id: str) -> CyclePlan:
        c = self.constraints.get(tls_id)
        total = sum(plan.green_times.values())
        clipped_green = {}
        for pid, gt in plan.green_times.items():
            clipped_green[pid] = max(c.min_green, min(c.max_green, gt))
        clipped_cycle = max(c.min_cycle, min(c.max_cycle, sum(clipped_green.values())))
        # Scale green times to fit clipped cycle
        scale = clipped_cycle / max(sum(clipped_green.values()), 1)
        if abs(scale - 1.0) > 0.01:
            clipped_green = {pid: gt * scale for pid, gt in clipped_green.items()}
        return CyclePlan(
            cycle_length=clipped_cycle,
            green_times=clipped_green,
            phase_order=plan.phase_order,
            offset_target=plan.offset_target,
        )

    def clip_phase_command(self, cmd: PhaseCommand, obs: NetworkObservation, plan: CyclePlan) -> PhaseCommand:
        tls_id = obs.ego.crossing_id
        c = self.constraints.get(tls_id)
        current = obs.ego.current_phase_id
        planned_duration = plan.green_times.get(cmd.next_phase_id, 30.0)
        clipped_duration = max(c.min_green, min(c.max_green, cmd.duration))
        # If extending current phase, cap the extension
        if cmd.action == "extend" and cmd.next_phase_id == current:
            max_dur = planned_duration + c.max_extend
            clipped_duration = min(clipped_duration, max_dur)
        if cmd.action == "shorten" and cmd.next_phase_id == current:
            min_dur = max(planned_duration - c.max_shorten, c.min_green)
            clipped_duration = max(clipped_duration, min_dur)
        return PhaseCommand(
            action=cmd.action,
            next_phase_id=cmd.next_phase_id,
            duration=clipped_duration,
            reason_code=cmd.reason_code,
        )
