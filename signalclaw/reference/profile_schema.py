"""SQL Reference Profile 数据结构。

定义从真实交通信号智能控制系统的 SQL 数据中提取的统计先验。
SQL 数据是参考性质的，不要求和 SUMO 场景一一对应。
SQL 提取统计先验，SUMO 使用这些先验构造更真实的仿真场景。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# 子先验数据结构
# ---------------------------------------------------------------------------


@dataclass
class CycleDurationPrior:
    """从 SQL 提取的周期时长先验。

    数据来源: crossing_base_rule, cycle_time, analyze_result
    """

    min_recommended: float = 60.0
    max_recommended: float = 180.0
    median: float = 135.0
    p25: float = 100.0
    p75: float = 160.0
    peak_median: float = 160.0
    offpeak_median: float = 100.0
    # 路口1全天默认周期 160s, 17:00 时段 180s
    # 路口10 周期 135s
    observed_values: List[float] = field(
        default_factory=lambda: [135.0, 160.0, 180.0]
    )


@dataclass
class PhaseGreenPrior:
    """从 SQL 提取的相位绿灯先验。

    数据来源: crossing_base_rule (phase_plan JSON), crossing_phase
    """

    min_green_recommended: float = 15.0
    max_green_recommended: float = 120.0
    typical_major_phase: List[float] = field(
        default_factory=lambda: [50.0, 60.0, 75.0]
    )
    typical_minor_phase: List[float] = field(
        default_factory=lambda: [20.0, 25.0, 35.0]
    )
    # 路口1: {"1":60, "2":25, "3":50, "4":25} -- 相位1为干道(60s), 2/4为次要(25s)
    # 路口1 17:00: 9号相位 20s
    # 路口10: {"3":75, "4":25, "9":35} -- 相位3为干道(75s)
    observed_major: List[float] = field(
        default_factory=lambda: [50.0, 60.0, 75.0]
    )
    observed_minor: List[float] = field(
        default_factory=lambda: [20.0, 25.0, 35.0]
    )


@dataclass
class MicroAdjustmentPrior:
    """从 SQL 提取的微调规律先验。

    数据来源: run_timing_adjustment (base_duration -> ai_duration -> micro_duration -> actual_duration)
    """

    max_extend_recommended: float = 8.0
    max_shorten_recommended: float = 5.0
    common_extend_seconds: List[float] = field(
        default_factory=lambda: [3.0, 5.0, 8.0]
    )
    common_shorten_seconds: List[float] = field(
        default_factory=lambda: [-3.0, -5.0]
    )
    high_confidence_extend_conditions: List[str] = field(
        default_factory=lambda: [
            "current_phase_queue_high",
            "downstream_not_blocked",
            "next_phase_pressure_low",
        ]
    )
    # micro_adjust_mode: 0=关, 1=AI微调, 2=算法微调
    # 路口1和路口10开启 AI 微调
    ai_micro_adjust_enabled: bool = True
    # micro_weight 默认10, 用于加权各相位的微调优先级
    default_micro_weight: int = 10


@dataclass
class DemandPatternPrior:
    """从 SQL 提取的流量时间分布先验。

    数据来源: traffic_flow_record, crossing_auto_control_plan
    """

    morning_peak_start: str = "07:30"
    morning_peak_end: str = "09:00"
    morning_peak_multiplier: float = 1.45
    evening_peak_start: str = "17:00"
    evening_peak_end: str = "19:00"
    evening_peak_multiplier: float = 1.60
    low_demand_multiplier: float = 0.35
    midday_multiplier: float = 0.75
    night_multiplier: float = 0.35
    directional_imbalance_ratios: List[float] = field(
        default_factory=lambda: [2.0, 3.0, 4.0]
    )
    # 控制时段: 路口1 07:35-19:20(工作日), 路口2 17:00-18:00(工作日), 路口10 07:00-20:00
    control_periods: List[Dict] = field(
        default_factory=lambda: [
            {
                "crossing_id": 1,
                "start": "07:35",
                "end": "19:20",
                "days": "Mon-Fri",
            },
            {
                "crossing_id": 2,
                "start": "17:00",
                "end": "18:00",
                "days": "Mon-Fri",
            },
            {
                "crossing_id": 10,
                "start": "07:00",
                "end": "20:00",
                "days": "Mon-Fri",
            },
        ]
    )


@dataclass
class PredictionErrorPrior:
    """从 SQL 提取的预测误差先验。

    数据来源: prediction_record (predicted_value vs actual)
    """

    horizon_seconds: List[int] = field(default_factory=lambda: [30, 60, 120])
    mape_30s: float = 0.12
    mape_60s: float = 0.18
    mape_120s: float = 0.27
    peak_error_multiplier: float = 1.3
    # 约27万条预测记录, 涵盖12个方向
    total_prediction_records: int = 270367
    direction_count: int = 12


@dataclass
class CoordinationPrior:
    """从 SQL 提取的协调模式先验。

    数据来源: wave, wave_beat, wave_node
    """

    use_one_hop_neighbors: bool = True
    offset_sensitive: bool = True
    downstream_spillback_sensitive: bool = True
    typical_offset_range_s: List[float] = field(
        default_factory=lambda: [0.0, 30.0]
    )
    green_wave_speed_ms: float = 13.9  # 50km/h
    # 1条绿波带, 2个节点, 支持硬切/软切/普通三种介入模式
    wave_types: List[str] = field(
        default_factory=lambda: ["green_wave", "red_wave"]
    )
    intervention_modes: List[str] = field(
        default_factory=lambda: ["hard_cut", "soft_cut", "normal"]
    )


@dataclass
class DirectionalImbalancePattern:
    """方向不均衡模式。

    来自真实路口的干道/支路车流差异。
    路口1(剑南大道): 干道车道数 48, 支路车道数 15
    路口2(盛邦街): 车道数 41-135 (大路口)
    路口10(新泽三路): 车道数 15-25
    """

    name: str
    description: str = ""
    major_minor_ratio: float = 2.0
    left_turn_multiplier: float = 1.0
    pulse_interval_s: float = 0.0
    pulse_width_s: float = 0.0


# ---------------------------------------------------------------------------
# 完整画像
# ---------------------------------------------------------------------------


@dataclass
class SQLReferenceProfile:
    """完整的 SQL 参考画像。

    从真实交通信号智能控制系统 SQL 数据中提取的统计先验。
    SQL 数据仅作为参考，不与 SUMO 场景强映射。
    """

    metadata: Dict = field(default_factory=dict)
    cycle_duration_prior: CycleDurationPrior = field(
        default_factory=CycleDurationPrior
    )
    phase_green_prior: PhaseGreenPrior = field(
        default_factory=PhaseGreenPrior
    )
    micro_adjustment_prior: MicroAdjustmentPrior = field(
        default_factory=MicroAdjustmentPrior
    )
    demand_patterns: DemandPatternPrior = field(
        default_factory=DemandPatternPrior
    )
    prediction_error_prior: PredictionErrorPrior = field(
        default_factory=PredictionErrorPrior
    )
    coordination_prior: CoordinationPrior = field(
        default_factory=CoordinationPrior
    )
    directional_imbalance_patterns: List[DirectionalImbalancePattern] = field(
        default_factory=list
    )

    # ------------------------------------------------------------------
    # 序列化 / 反序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """序列化为 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SQLReferenceProfile:
        """从 dict 反序列化。"""
        metadata = d.get("metadata", {})

        cycle_raw = d.get("cycle_duration_prior", {})
        cycle_prior = CycleDurationPrior(**{
            k: v for k, v in cycle_raw.items()
            if k in CycleDurationPrior.__dataclass_fields__
        })

        phase_raw = d.get("phase_green_prior", {})
        phase_prior = PhaseGreenPrior(**{
            k: v for k, v in phase_raw.items()
            if k in PhaseGreenPrior.__dataclass_fields__
        })

        micro_raw = d.get("micro_adjustment_prior", {})
        micro_prior = MicroAdjustmentPrior(**{
            k: v for k, v in micro_raw.items()
            if k in MicroAdjustmentPrior.__dataclass_fields__
        })

        demand_raw = d.get("demand_patterns", {})
        demand_prior = DemandPatternPrior(**{
            k: v for k, v in demand_raw.items()
            if k in DemandPatternPrior.__dataclass_fields__
        })

        pred_raw = d.get("prediction_error_prior", {})
        pred_prior = PredictionErrorPrior(**{
            k: v for k, v in pred_raw.items()
            if k in PredictionErrorPrior.__dataclass_fields__
        })

        coord_raw = d.get("coordination_prior", {})
        coord_prior = CoordinationPrior(**{
            k: v for k, v in coord_raw.items()
            if k in CoordinationPrior.__dataclass_fields__
        })

        dir_raw_list = d.get("directional_imbalance_patterns", [])
        dir_patterns = [
            DirectionalImbalancePattern(**{
                k: v for k, v in dp.items()
                if k in DirectionalImbalancePattern.__dataclass_fields__
            })
            for dp in dir_raw_list
        ]

        return cls(
            metadata=metadata,
            cycle_duration_prior=cycle_prior,
            phase_green_prior=phase_prior,
            micro_adjustment_prior=micro_prior,
            demand_patterns=demand_prior,
            prediction_error_prior=pred_prior,
            coordination_prior=coord_prior,
            directional_imbalance_patterns=dir_patterns,
        )

    def save(self, path: str) -> None:
        """保存为 JSON 文件。"""
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> SQLReferenceProfile:
        """从 JSON 文件加载。"""
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def to_prompt_text(self) -> str:
        """转换为可注入 GLM prompt 的文本。

        将统计先验以简洁可读的方式呈现，供 GLM 在进化时参考。
        """
        lines = []
        lines.append("## SQL 真实数据统计先验（参考性质，非强制约束）")
        lines.append("")

        cp = self.cycle_duration_prior
        lines.append("### 周期时长参考")
        lines.append(f"- 推荐范围: {cp.min_recommended:.0f} ~ {cp.max_recommended:.0f}s")
        lines.append(f"- 中位数: {cp.median:.0f}s (P25={cp.p25:.0f}s, P75={cp.p75:.0f}s)")
        lines.append(f"- 高峰中位数: {cp.peak_median:.0f}s, 低峰中位数: {cp.offpeak_median:.0f}s")
        if cp.observed_values:
            lines.append(f"- 实际观测值: {', '.join(f'{v:.0f}s' for v in cp.observed_values)}")
        lines.append("")

        pg = self.phase_green_prior
        lines.append("### 相位绿灯参考")
        lines.append(f"- 推荐范围: {pg.min_green_recommended:.0f} ~ {pg.max_green_recommended:.0f}s")
        lines.append(f"- 干道相位典型值: {', '.join(f'{v:.0f}s' for v in pg.typical_major_phase)}")
        lines.append(f"- 次要相位典型值: {', '.join(f'{v:.0f}s' for v in pg.typical_minor_phase)}")
        lines.append("")

        ma = self.micro_adjustment_prior
        lines.append("### 微调幅度参考")
        lines.append(f"- 最大延长: {ma.max_extend_recommended:.0f}s")
        lines.append(f"- 最大缩短: {ma.max_shorten_recommended:.0f}s")
        lines.append(f"- 常见延长值: {', '.join(f'{v:.0f}s' for v in ma.common_extend_seconds)}")
        lines.append(f"- 常见缩短值: {', '.join(f'{v:.0f}s' for v in ma.common_shorten_seconds)}")
        if ma.ai_micro_adjust_enabled:
            lines.append("- 系统使用 AI 微调模式")
        lines.append("")

        dp = self.demand_patterns
        lines.append("### 流量时间分布参考")
        lines.append(f"- 早高峰: {dp.morning_peak_start}-{dp.morning_peak_end} (倍率 {dp.morning_peak_multiplier:.2f}x)")
        lines.append(f"- 晚高峰: {dp.evening_peak_start}-{dp.evening_peak_end} (倍率 {dp.evening_peak_multiplier:.2f}x)")
        lines.append(f"- 平峰: {dp.midday_multiplier:.2f}x, 低峰: {dp.low_demand_multiplier:.2f}x")
        lines.append(f"- 方向不均衡比: {', '.join(f'{r:.1f}' for r in dp.directional_imbalance_ratios)}")
        lines.append("")

        pe = self.prediction_error_prior
        lines.append("### 预测误差参考")
        lines.append(f"- 30s MAPE: {pe.mape_30s:.0%}, 60s MAPE: {pe.mape_60s:.0%}, 120s MAPE: {pe.mape_120s:.0%}")
        lines.append(f"- 高峰误差倍率: {pe.peak_error_multiplier:.1f}x")
        lines.append("")

        co = self.coordination_prior
        lines.append("### 协调模式参考")
        lines.append(f"- 绿波速度: {co.green_wave_speed_ms:.1f} m/s ({co.green_wave_speed_ms * 3.6:.0f} km/h)")
        lines.append(f"- 相位差范围: {co.typical_offset_range_s[0]:.0f} ~ {co.typical_offset_range_s[-1]:.0f}s")
        if co.use_one_hop_neighbors:
            lines.append("- 使用一跳邻居路口信息协调")
        lines.append("")

        if self.directional_imbalance_patterns:
            lines.append("### 方向不均衡模式")
            for pat in self.directional_imbalance_patterns:
                lines.append(f"- {pat.name}: 主/次比={pat.major_minor_ratio:.1f}, {pat.description}")
            lines.append("")

        lines.append("注意: 以上为先验统计参考，不要求 SUMO 场景严格匹配。")
        return "\n".join(lines)
