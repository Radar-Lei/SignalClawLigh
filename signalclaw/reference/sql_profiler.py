"""SQL Reference Profiler - 从 SQL 数据提取统计先验。

基于真实交通信号智能控制系统的数据构建参考画像。
SQL 数据是参考性质的，不要求和 SUMO 场景一一对应。

数据来源:
  - traffic_full_20260521_112701.sql (5.0GB)
  - 3 个真实路口，6 个月数据 (2025-11 ~ 2026-05)
  - 约 1.02 亿条流量记录，27 万条 AI 预测，10.5 万条配时调整

由于 SQL 文件过大 (5GB)，本模块主要基于 docs/交叉口数据说明.md 中的
已知路口信息构建合理的默认先验，预留从 SQL 提取的接口。
"""

from __future__ import annotations

import os
from typing import Optional

from signalclaw.reference.profile_schema import (
    CoordinationPrior,
    CycleDurationPrior,
    DemandPatternPrior,
    DirectionalImbalancePattern,
    MicroAdjustmentPrior,
    PhaseGreenPrior,
    PredictionErrorPrior,
    SQLReferenceProfile,
)


class SQLReferenceProfiler:
    """从 SQL 数据提取交通系统统计先验。"""

    def __init__(self, sql_path: Optional[str] = None):
        """
        Args:
            sql_path: SQL 文件路径（可选，如果 None 则使用默认先验）。
                      实际 SQL 文件 5GB，当前版本不支持直接解析，
                      使用基于文档信息的默认先验。
        """
        self.sql_path = sql_path

    def build_profile(self) -> SQLReferenceProfile:
        """构建 SQL 参考画像。

        如果有 SQL 文件且可访问，从数据中提取统计信息；
        否则根据 docs/交叉口数据说明.md 中的信息使用合理的默认值。
        """
        if self.sql_path and os.path.exists(self.sql_path):
            return self._profile_from_sql()
        return self._default_profile()

    def _default_profile(self) -> SQLReferenceProfile:
        """基于文档信息的默认先验。

        数据来源: docs/交叉口数据说明.md
        路口信息:
          - 路口1 (剑南大道与府城大道): 5相位, 干道车道48, AI调控开启
          - 路口2 (盛邦街): 2相位, 车道41-135, AI调控关闭
          - 路口10 (新泽三路-锦和西二街): 3相位, 车道15-25, AI调控开启

        配时规则:
          - 路口1全天默认: {"1":60, "2":25, "3":50, "4":25} 周期160s
          - 路口1 17:00: 增加9号相位(20s), 周期180s
          - 路口10: {"3":75, "4":25, "9":35} 周期135s

        控制时段:
          - 路口1: 07:35-19:20 (工作日)
          - 路口2: 17:00-18:00 (工作日)
          - 路口10: 07:00-20:00 (工作日)
        """
        # ---- 周期时长先验 ----
        cycle_prior = CycleDurationPrior(
            min_recommended=60.0,
            max_recommended=180.0,
            median=135.0,
            p25=100.0,
            p75=160.0,
            peak_median=160.0,
            offpeak_median=100.0,
            observed_values=[135.0, 160.0, 180.0],
        )

        # ---- 相位绿灯先验 ----
        phase_prior = PhaseGreenPrior(
            min_green_recommended=15.0,
            max_green_recommended=120.0,
            typical_major_phase=[50.0, 60.0, 75.0],
            typical_minor_phase=[20.0, 25.0, 35.0],
            observed_major=[50.0, 60.0, 75.0],
            observed_minor=[20.0, 25.0, 35.0],
        )

        # ---- 微调先验 ----
        micro_prior = MicroAdjustmentPrior(
            max_extend_recommended=8.0,
            max_shorten_recommended=5.0,
            common_extend_seconds=[3.0, 5.0, 8.0],
            common_shorten_seconds=[-3.0, -5.0],
            high_confidence_extend_conditions=[
                "current_phase_queue_high",
                "downstream_not_blocked",
                "next_phase_pressure_low",
            ],
            ai_micro_adjust_enabled=True,
            default_micro_weight=10,
        )

        # ---- 流量时间分布先验 ----
        demand_prior = DemandPatternPrior(
            morning_peak_start="07:30",
            morning_peak_end="09:00",
            morning_peak_multiplier=1.45,
            evening_peak_start="17:00",
            evening_peak_end="19:00",
            evening_peak_multiplier=1.60,
            low_demand_multiplier=0.35,
            midday_multiplier=0.75,
            night_multiplier=0.35,
            directional_imbalance_ratios=[2.0, 3.0, 4.0],
            control_periods=[
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
            ],
        )

        # ---- 预测误差先验 ----
        pred_prior = PredictionErrorPrior(
            horizon_seconds=[30, 60, 120],
            mape_30s=0.12,
            mape_60s=0.18,
            mape_120s=0.27,
            peak_error_multiplier=1.3,
            total_prediction_records=270367,
            direction_count=12,
        )

        # ---- 协调先验 ----
        coord_prior = CoordinationPrior(
            use_one_hop_neighbors=True,
            offset_sensitive=True,
            downstream_spillback_sensitive=True,
            typical_offset_range_s=[0.0, 30.0],
            green_wave_speed_ms=13.9,
            wave_types=["green_wave", "red_wave"],
            intervention_modes=["hard_cut", "soft_cut", "normal"],
        )

        # ---- 方向不均衡模式 ----
        dir_patterns = [
            DirectionalImbalancePattern(
                name="major_arterial",
                description="干道 vs 支路，如剑南大道(48车道) vs 支路(15车道)",
                major_minor_ratio=3.2,
                left_turn_multiplier=0.8,
            ),
            DirectionalImbalancePattern(
                name="tide_flow",
                description="潮汐流，早高峰进城方向 / 晚高峰出城方向",
                major_minor_ratio=2.5,
                pulse_interval_s=60.0,
                pulse_width_s=20.0,
            ),
            DirectionalImbalancePattern(
                name="balanced_suburban",
                description="近均衡路口，如新泽三路(15-25车道)",
                major_minor_ratio=1.5,
            ),
        ]

        # ---- 元数据 ----
        metadata = {
            "source": "traffic_full_20260521_112701.sql",
            "data_period": "2025-11 ~ 2026-05",
            "crossing_count": 3,
            "total_flow_records": 101976509,
            "total_prediction_records": 270367,
            "total_adjustment_records": 105329,
            "total_analyze_records": 19655,
            "intersection_details": [
                {
                    "id": 1,
                    "name": "剑南大道与府城大道交叉口",
                    "real_id": "81231",
                    "ai_enabled": True,
                    "phase_count": 5,
                    "phase_ids": [1, 2, 3, 4, 9],
                    "lane_count_range": [15, 48],
                    "micro_adjust_mode": "AI微调",
                    "signal_dispatch_mode": "相位计划+相位下发",
                    "control_period": "07:35-19:20 (Mon-Fri)",
                    "base_cycle": {"1": 60, "2": 25, "3": 50, "4": 25},
                },
                {
                    "id": 2,
                    "name": "盛邦街",
                    "real_id": "94122",
                    "ai_enabled": False,
                    "phase_count": 2,
                    "phase_ids": [5, 6],
                    "lane_count_range": [41, 135],
                    "micro_adjust_mode": "关闭",
                    "control_period": "17:00-18:00 (Mon-Fri)",
                },
                {
                    "id": 10,
                    "name": "新泽三路-锦和西二街",
                    "real_id": "51010000100134",
                    "ai_enabled": True,
                    "phase_count": 3,
                    "phase_ids": [3, 4, 9],
                    "lane_count_range": [15, 25],
                    "micro_adjust_mode": "AI微调",
                    "signal_dispatch_mode": "相位计划+相位下发",
                    "control_period": "07:00-20:00 (Mon-Fri)",
                    "base_cycle": {"3": 75, "4": 25, "9": 35},
                },
            ],
        }

        return SQLReferenceProfile(
            metadata=metadata,
            cycle_duration_prior=cycle_prior,
            phase_green_prior=phase_prior,
            micro_adjustment_prior=micro_prior,
            demand_patterns=demand_prior,
            prediction_error_prior=pred_prior,
            coordination_prior=coord_prior,
            directional_imbalance_patterns=dir_patterns,
        )

    def _profile_from_sql(self) -> SQLReferenceProfile:
        """从 SQL 数据提取统计先验（预留接口）。

        如果将来有 SQL 数据可访问（例如通过数据库连接而非直接解析 SQL 文件），
        可以实现此方法，从以下表中提取真实统计信息：
          - cycle_time: 周期时长分布
          - run_timing_adjustment: 微调幅度分布
          - traffic_flow_record: 流量时间分布
          - prediction_record: 预测误差统计

        当前版本返回基于文档的默认先验。
        """
        return self._default_profile()
