"""预测噪声生成器。

根据 SQL 预测误差先验，在 SUMO 仿真中添加预测噪声，
模拟真实系统中的预测误差。

误差模型基于真实交通预测系统的 MAPE 特征：
- 30s 预测: MAPE ≈ 12%
- 60s 预测: MAPE ≈ 18%
- 120s 预测: MAPE ≈ 27%
- 高峰期误差额外增加约 30%
"""

from __future__ import annotations

import math
import random
from typing import Optional


class PredictionNoiseGenerator:
    """根据 SQL 预测误差先验，在 SUMO 仿真中添加预测噪声。

    误差模型假设：
    - MAPE 随预测时距近似线性增长
    - 高峰期误差额外放大
    - 噪声服从正态分布，均值为 0，标准差由 MAPE 决定

    使用方式：
    >>> png = PredictionNoiseGenerator()
    >>> noisy = png.add_noise(actual_queue=15.0, horizon_s=60.0, is_peak=True)
    """

    def __init__(
        self,
        mape_30s: float = 0.12,
        mape_60s: float = 0.18,
        mape_120s: float = 0.27,
        peak_multiplier: float = 1.3,
    ):
        """
        Parameters
        ----------
        mape_30s : float
            30 秒预测时距的 MAPE，默认 0.12
        mape_60s : float
            60 秒预测时距的 MAPE，默认 0.18
        mape_120s : float
            120 秒预测时距的 MAPE，默认 0.27
        peak_multiplier : float
            高峰期误差放大系数，默认 1.3
        """
        self.mape_30s = mape_30s
        self.mape_60s = mape_60s
        self.mape_120s = mape_120s
        self.peak_multiplier = peak_multiplier

        # 基于三个标定点拟合线性模型: mape = a * horizon_s + b
        # 使用最小二乘拟合
        self._fit_error_model()

    def _fit_error_model(self) -> None:
        """基于标定数据拟合误差模型。

        使用三个标定点 (30, mape_30s), (60, mape_60s), (120, mape_120s)
        拟合线性关系 mape = a * horizon_s + b
        """
        x1, y1 = 30.0, self.mape_30s
        x2, y2 = 60.0, self.mape_60s
        x3, y3 = 120.0, self.mape_120s

        # 最小二乘线性拟合
        n = 3
        sum_x = x1 + x2 + x3
        sum_y = y1 + y2 + y3
        sum_xy = x1 * y1 + x2 * y2 + x3 * y3
        sum_x2 = x1 ** 2 + x2 ** 2 + x3 ** 2

        denom = n * sum_x2 - sum_x ** 2
        if abs(denom) < 1e-12:
            self._slope = 0.001
            self._intercept = self.mape_60s
        else:
            self._slope = (n * sum_xy - sum_x * sum_y) / denom
            self._intercept = (sum_y - self._slope * sum_x) / n

    def _get_mape(self, horizon_s: float) -> float:
        """根据预测时距计算 MAPE。

        Parameters
        ----------
        horizon_s : float
            预测时距（秒）

        Returns
        -------
        float
            对应的 MAPE
        """
        if horizon_s <= 0:
            return 0.0
        mape = self._slope * horizon_s + self._intercept
        return max(0.0, mape)

    def add_noise(
        self,
        actual_value: float,
        horizon_s: float,
        is_peak: bool = False,
        seed: Optional[int] = None,
    ) -> float:
        """根据预测时距和是否高峰添加噪声。

        Parameters
        ----------
        actual_value : float
            真实值（如排队长度、流量等）
        horizon_s : float
            预测时距（秒）
        is_peak : bool
            是否处于高峰期
        seed : int, optional
            随机种子

        Returns
        -------
        float
            添加噪声后的预测值（非负）
        """
        if actual_value <= 0:
            return 0.0

        if seed is not None:
            rng = random.Random(seed)
        else:
            rng = random.Random()

        # 获取基础 MAPE
        base_mape = self._get_mape(horizon_s)

        # 高峰期放大
        effective_mape = base_mape * (self.peak_multiplier if is_peak else 1.0)

        # 生成正态噪声（标准差 = MAPE * actual_value）
        std = effective_mape * actual_value
        noise = rng.gauss(0, std)

        # 添加噪声并确保非负
        noisy_value = actual_value + noise
        return max(0.0, noisy_value)

    def add_noise_vector(
        self,
        actual_values: list[float],
        horizon_s: float,
        is_peak: bool = False,
        seed: Optional[int] = None,
    ) -> list[float]:
        """为向量值批量添加噪声。

        Parameters
        ----------
        actual_values : list[float]
            真实值列表
        horizon_s : float
            预测时距（秒）
        is_peak : bool
            是否处于高峰期
        seed : int, optional
            随机种子

        Returns
        -------
        list[float]
            添加噪声后的预测值列表
        """
        return [
            self.add_noise(v, horizon_s, is_peak, seed=(seed + i if seed is not None else None))
            for i, v in enumerate(actual_values)
        ]

    def get_error_stats(self, horizon_s: float, is_peak: bool = False) -> dict:
        """获取指定条件下的误差统计信息。

        Parameters
        ----------
        horizon_s : float
            预测时距（秒）
        is_peak : bool
            是否高峰期

        Returns
        -------
        dict
            包含 mape, std_ratio 等信息
        """
        mape = self._get_mape(horizon_s)
        if is_peak:
            mape *= self.peak_multiplier

        return {
            "horizon_s": horizon_s,
            "is_peak": is_peak,
            "mape": round(mape, 4),
            "std_ratio": round(mape, 4),  # 标准差与真实值的比值
            "peak_multiplier": self.peak_multiplier if is_peak else 1.0,
        }
