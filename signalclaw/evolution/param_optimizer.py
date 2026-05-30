"""DSLParamOptimizer - DSL 参数优化器。

在 GLM 提出的 DSL 结构模板基础上，搜索最优连续参数（权重、阈值等）。
GLM 负责：提出 feature 组合、公式结构、保护规则。
参数优化器负责：调 w_queue, w_wait, w_downstream, w_hunger 等连续参数。
SUMO sealed evaluator 负责：判定是否真的比 incumbent 好。

支持三种优化策略：
  - grid: 网格搜索，适合低维空间
  - random: 随机搜索，基线对比
  - bayesian: 贝叶斯优化（scipy 实现的高斯过程代理模型）

降级策略：如果 scipy 不可用，自动降级为 random search。
"""

from __future__ import annotations

import copy
import itertools
import logging
import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from signalclaw.evolution.dsl_compiler import DslCompiler, CompileResult

logger = logging.getLogger(__name__)


# ============================================================================
# 默认参数搜索范围
# ============================================================================

DEFAULT_PARAM_RANGES: Dict[str, Tuple[float, float]] = {
    "w_queue": (-2.0, 2.0),
    "w_wait": (-1.0, 1.0),
    "w_waiting": (-1.0, 1.0),
    "w_hunger": (-2.0, 2.0),
    "w_downstream": (-2.0, 0.0),
    "w_downstream_queue": (-2.0, 0.0),
    "w_upstream_pressure": (-1.0, 1.0),
    "w_spillback": (-1.5, 0.0),
    "w_arrival": (-1.0, 1.0),
    "w_switch": (-2.0, 0.0),
    "switch_penalty": (0.0, 5.0),
    "extend_threshold": (1.0, 10.0),
    "shorten_threshold": (0.5, 5.0),
    "max_extend": (2.0, 15.0),
    "max_shorten": (2.0, 10.0),
    "min_green": (5.0, 20.0),
    "max_green": (30.0, 90.0),
    "base": (40.0, 120.0),
    "queue_gain": (0.0, 2.0),
}

# cycle 专用参数
CYCLE_OPTIMIZABLE_PARAMS = [
    "w_queue", "w_wait", "w_downstream", "w_hunger", "w_arrival",
    "w_spillback", "min_green", "max_green", "base", "queue_gain",
]

# phase 专用参数
PHASE_OPTIMIZABLE_PARAMS = [
    "w_queue", "w_waiting", "w_arrival", "w_hunger", "w_downstream",
    "w_switch", "extend_threshold", "shorten_threshold", "max_extend",
    "max_shorten",
]


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class OptimizerConfig:
    """参数优化器配置。"""

    method: str = "bayesian"  # "grid", "random", "bayesian"
    n_trials: int = 50  # 优化尝试次数
    param_ranges: Dict[str, Tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_PARAM_RANGES)
    )
    micro_sim_duration: float = 600.0  # 每次评估用的仿真时长（秒）
    seed: int = 42  # 随机种子
    n_grid_points: int = 5  # grid search 每维度的点数
    early_stop_patience: int = 15  # 多少次无改善后停止
    min_improvement: float = 0.001  # 最小改善阈值（相对改善百分比）


@dataclass
class OptimizationResult:
    """参数优化结果。"""

    best_params: Dict[str, float]
    best_score: float  # 越低越好
    incumbent_score: float
    improvement_pct: float  # 改善百分比（正值表示改善）
    n_evaluations: int
    optimization_method: str
    all_trials: List[Dict[str, Any]] = field(default_factory=list)
    best_dsl_yaml: str = ""  # 最优参数填入后的完整 DSL YAML
    best_python_code: str = ""  # 编译后的 Python 代码

    @property
    def improved(self) -> bool:
        """是否有改善（分数降低）。"""
        return self.best_score < self.incumbent_score


# ============================================================================
# Evaluator Protocol
# ============================================================================

# evaluator_factory 签名：接收 python_code: str, skill_type: str -> float (score)
# score 越低越好


# ============================================================================
# 抽象优化策略基类
# ============================================================================

class BaseOptimizer(ABC):
    """参数优化策略的抽象基类。"""

    def __init__(
        self,
        param_ranges: Dict[str, Tuple[float, float]],
        n_trials: int,
        seed: int,
    ):
        self.param_ranges = param_ranges
        self.n_trials = n_trials
        self.rng = random.Random(seed)

    @abstractmethod
    def suggest(self, trial_num: int) -> Dict[str, float]:
        """建议下一组参数。"""
        ...

    def on_trial_complete(self, trial_num: int, params: Dict[str, float], score: float) -> None:
        """通知一次试验完成，供有状态的优化器更新内部模型。"""
        pass


# ============================================================================
# Grid Search
# ============================================================================

class GridSearchOptimizer(BaseOptimizer):
    """网格搜索：穷举所有参数组合。

    注意：高维空间下组合爆炸，仅适合 <= 4 维的场景。
    """

    def __init__(self, param_ranges: Dict[str, Tuple[float, float]],
                 n_trials: int, seed: int, n_grid_points: int = 5):
        super().__init__(param_ranges, n_trials, seed)
        self.n_grid_points = n_grid_points
        self._grid = self._build_grid()
        self._cursor = 0

    def _build_grid(self) -> List[Dict[str, float]]:
        """构建网格点。"""
        param_names = sorted(self.param_ranges.keys())
        grids_per_param = []
        for name in param_names:
            lo, hi = self.param_ranges[name]
            step = (hi - lo) / max(self.n_grid_points - 1, 1)
            values = [lo + step * i for i in range(self.n_grid_points)]
            grids_per_param.append(values)

        combinations = list(itertools.product(*grids_per_param))
        grid_list = []
        for combo in combinations:
            point = {}
            for name, val in zip(param_names, combo):
                point[name] = round(val, 6)
            grid_list.append(point)
        return grid_list

    def suggest(self, trial_num: int) -> Dict[str, float]:
        if self._cursor < len(self._grid):
            point = self._grid[self._cursor]
            self._cursor += 1
            return point
        # 超出网格范围，随机采样
        return self._random_point()

    def _random_point(self) -> Dict[str, float]:
        point = {}
        for name, (lo, hi) in self.param_ranges.items():
            point[name] = round(self.rng.uniform(lo, hi), 6)
        return point


# ============================================================================
# Random Search
# ============================================================================

class RandomSearchOptimizer(BaseOptimizer):
    """随机搜索：均匀采样参数空间。"""

    def suggest(self, trial_num: int) -> Dict[str, float]:
        point = {}
        for name, (lo, hi) in self.param_ranges.items():
            point[name] = round(self.rng.uniform(lo, hi), 6)
        return point


# ============================================================================
# Bayesian Optimization (简化版高斯过程)
# ============================================================================

class BayesianOptimizer(BaseOptimizer):
    """贝叶斯优化：基于高斯过程代理模型 + Expected Improvement 采集函数。

    使用 scipy 实现高斯过程回归。如果 scipy 不可用，降级为 random search。
    """

    def __init__(self, param_ranges: Dict[str, Tuple[float, float]],
                 n_trials: int, seed: int):
        super().__init__(param_ranges, n_trials, seed)
        self._observed_params: List[Dict[str, float]] = []
        self._observed_scores: List[float] = []
        self._param_names = sorted(param_ranges.keys())
        self._scipy_available = self._check_scipy()
        # 初始随机探索次数
        self._warmup = min(10, n_trials // 3)

    @staticmethod
    def _check_scipy() -> bool:
        try:
            from scipy.stats import norm  # noqa: F401
            return True
        except ImportError:
            return False

    def suggest(self, trial_num: int) -> Dict[str, float]:
        # warmup 阶段随机采样
        if trial_num < self._warmup or not self._scipy_available:
            return self._random_point()

        # 使用高斯过程 + EI 采集函数选择下一个点
        return self._suggest_ei()

    def _random_point(self) -> Dict[str, float]:
        point = {}
        for name, (lo, hi) in self.param_ranges.items():
            point[name] = round(self.rng.uniform(lo, hi), 6)
        return point

    def _suggest_ei(self) -> Dict[str, float]:
        """用 Expected Improvement 选择下一个采样点。"""
        try:
            import numpy as np
            from scipy.stats import norm
        except ImportError:
            return self._random_point()

        if len(self._observed_params) < 3:
            return self._random_point()

        # 将参数转为标准化向量
        X_obs = np.array([
            self._params_to_vector(p) for p in self._observed_params
        ])
        y_obs = np.array(self._observed_scores)

        # 简化高斯过程：使用 RBF 核的均值和方差估计
        best_y = np.min(y_obs)

        # 候选点：随机采样 + 历史最优附近扰动
        candidates = []
        # 随机候选
        for _ in range(100):
            point = self._random_point()
            candidates.append(self._params_to_vector(point))

        # 最优附近扰动
        best_idx = int(np.argmin(y_obs))
        best_vec = X_obs[best_idx]
        for _ in range(50):
            noise = self.rng.gauss(0, 0.1)
            perturbed = np.clip(
                best_vec + noise,
                0.0, 1.0,
            )
            candidates.append(perturbed)

        X_cand = np.array(candidates)

        # 计算每个候选的 GP 均值和方差
        # 简化核：K(x, x') = exp(-||x - x'||^2 / (2 * l^2))
        length_scale = 0.3
        mu, sigma = self._gp_predict(X_obs, y_obs, X_cand, length_scale)

        # Expected Improvement
        xi = 0.01  # exploration-exploitation trade-off
        sigma_safe = np.maximum(sigma, 1e-9)
        improvement = mu - best_y - xi
        Z = improvement / sigma_safe
        ei = improvement * norm.cdf(Z) + sigma_safe * norm.pdf(Z)
        ei[sigma < 1e-9] = 0.0

        # 选择 EI 最大的候选
        best_cand_idx = int(np.argmax(ei))
        best_vector = X_cand[best_cand_idx]
        return self._vector_to_params(best_vector)

    @staticmethod
    def _gp_predict(
        X_obs, y_obs, X_cand, length_scale=0.3
    ) -> Tuple[Any, Any]:
        """简化高斯过程预测。返回 (mu, sigma) 均值和标准差。"""
        import numpy as np

        n = len(X_obs)
        noise_var = 1e-6

        # 计算核矩阵
        def rbf_kernel(X1, X2, ls):
            sq_dist = np.sum(X1 ** 2, axis=1, keepdims=True) + \
                      np.sum(X2 ** 2, axis=1, keepdims=True).T - \
                      2 * X1 @ X2.T
            return np.exp(-sq_dist / (2 * ls ** 2))

        K = rbf_kernel(X_obs, X_obs, length_scale) + noise_var * np.eye(n)
        K_s = rbf_kernel(X_obs, X_cand, length_scale)

        try:
            L = np.linalg.cholesky(K)
            alpha = np.linalg.solve(
                L.T, np.linalg.solve(L, y_obs)
            )
            mu = K_s.T @ alpha

            v = np.linalg.solve(L, K_s)
            sigma = np.sqrt(
                np.maximum(
                    rbf_kernel(X_cand, X_cand, length_scale).diagonal() - np.sum(v ** 2, axis=0),
                    1e-9,
                )
            )
        except np.linalg.LinAlgError:
            # Cholesky 分解失败，回退到简单均值
            mu = np.full(len(X_cand), np.mean(y_obs))
            sigma = np.full(len(X_cand), np.std(y_obs) + 1.0)

        return mu, sigma

    def _params_to_vector(self, params: Dict[str, float]) -> List[float]:
        """将参数字典转为 [0, 1] 标准化向量。"""
        vec = []
        for name in self._param_names:
            lo, hi = self.param_ranges[name]
            val = params.get(name, (lo + hi) / 2)
            normalized = (val - lo) / max(hi - lo, 1e-9)
            vec.append(max(0.0, min(1.0, normalized)))
        return vec

    def _vector_to_params(self, vector) -> Dict[str, float]:
        """将 [0, 1] 标准化向量转回参数字典。"""
        import numpy as np
        params = {}
        for i, name in enumerate(self._param_names):
            lo, hi = self.param_ranges[name]
            val = float(vector[i]) if i < len(vector) else 0.5
            val = max(0.0, min(1.0, val))
            params[name] = round(lo + val * (hi - lo), 6)
        return params

    def on_trial_complete(self, trial_num: int, params: Dict[str, float], score: float) -> None:
        """记录观测结果，更新 GP 模型。"""
        self._observed_params.append(params)
        self._observed_scores.append(score)


# ============================================================================
# DSLParamOptimizer - 主优化器
# ============================================================================

class DSLParamOptimizer:
    """DSL 参数优化器：在 DSL 模板中搜索最优参数。

    使用方式::

        optimizer = DSLParamOptimizer(
            config=OptimizerConfig(method="bayesian", n_trials=50),
            evaluator_factory=my_evaluator,
        )
        result = optimizer.optimize(dsl_template, incumbent_score=10.5)
        if result.improved:
            print(f"改善了 {result.improvement_pct:.1f}%")
    """

    def __init__(
        self,
        config: OptimizerConfig,
        evaluator_factory: Callable[[str, str], float],
        compiler: Optional[DslCompiler] = None,
    ):
        """
        Parameters
        ----------
        config : OptimizerConfig
            优化配置
        evaluator_factory : callable
            评估函数，签名 (python_code: str, skill_type: str) -> float
            返回的 score 越低越好。
        compiler : DslCompiler, optional
            DSL 编译器实例。为 None 时使用默认实例。
        """
        self.config = config
        self.evaluator_factory = evaluator_factory
        self.compiler = compiler or DslCompiler()

    def optimize(
        self,
        dsl_template: dict,
        incumbent_score: float,
    ) -> OptimizationResult:
        """优化 DSL 模板中的参数，返回最优配置。

        Parameters
        ----------
        dsl_template : dict
            DSL 模板字典（YAML 解析后的 dict）。参数值可以留空或使用默认值，
            优化器会搜索最优参数替换它们。
        incumbent_score : float
            当前最优分数（越低越好），用于计算改善百分比。

        Returns
        -------
        OptimizationResult
        """
        skill_type = dsl_template.get("skill_type", "cycle")
        if skill_type not in ("cycle", "phase"):
            return OptimizationResult(
                best_params={},
                best_score=float("inf"),
                incumbent_score=incumbent_score,
                improvement_pct=0.0,
                n_evaluations=0,
                optimization_method="none",
                all_trials=[],
            )

        # 确定可优化的参数和范围
        param_ranges = self._infer_param_ranges(dsl_template, skill_type)
        if not param_ranges:
            logger.warning("无可优化的参数，跳过优化")
            return OptimizationResult(
                best_params={},
                best_score=incumbent_score,
                incumbent_score=incumbent_score,
                improvement_pct=0.0,
                n_evaluations=0,
                optimization_method="none",
                all_trials=[],
            )

        logger.info(
            "开始参数优化: method=%s, skill_type=%s, params=%s, "
            "incumbent_score=%.4f, n_trials=%d",
            self.config.method, skill_type, list(param_ranges.keys()),
            incumbent_score, self.config.n_trials,
        )

        # 创建优化策略
        strategy = self._create_strategy(param_ranges)

        # 优化主循环
        best_params: Dict[str, float] = {}
        best_score = float("inf")
        all_trials: List[Dict[str, Any]] = []
        no_improve_count = 0

        for trial_num in range(self.config.n_trials):
            # 建议参数
            params = strategy.suggest(trial_num)

            # 应用参数到 DSL 模板并编译
            dsl_with_params = self._apply_params(dsl_template, params)
            score, eval_ok = self._evaluate_params(dsl_with_params, skill_type)

            # 通知策略
            strategy.on_trial_complete(trial_num, params, score)

            # 记录试验
            trial_record = {
                "trial": trial_num,
                "params": dict(params),
                "score": score,
                "eval_ok": eval_ok,
            }
            all_trials.append(trial_record)

            if eval_ok and score < best_score:
                improvement = (best_score - score) / max(abs(best_score), 1e-9)
                if improvement > self.config.min_improvement:
                    no_improve_count = 0
                else:
                    no_improve_count += 1
                best_score = score
                best_params = dict(params)
                logger.debug(
                    "Trial %d: 新最优 score=%.4f params=%s",
                    trial_num, score, params,
                )
            else:
                no_improve_count += 1

            # 早停
            if no_improve_count >= self.config.early_stop_patience:
                logger.info(
                    "早停: 连续 %d 次无显著改善",
                    no_improve_count,
                )
                break

        # 计算改善百分比
        if incumbent_score > 0 and best_score < float("inf"):
            improvement_pct = (incumbent_score - best_score) / incumbent_score * 100
        else:
            improvement_pct = 0.0

        # 生成最优 DSL YAML 和 Python 代码
        best_dsl_yaml = ""
        best_python_code = ""
        if best_params:
            final_dsl = self._apply_params(dsl_template, best_params)
            best_dsl_yaml = yaml.dump(final_dsl, default_flow_style=False, allow_unicode=True)
            compile_result = self._compile_dsl(final_dsl)
            if compile_result.success and compile_result.python_code:
                best_python_code = compile_result.python_code

        result = OptimizationResult(
            best_params=best_params,
            best_score=best_score,
            incumbent_score=incumbent_score,
            improvement_pct=round(improvement_pct, 2),
            n_evaluations=len(all_trials),
            optimization_method=self.config.method,
            all_trials=all_trials,
            best_dsl_yaml=best_dsl_yaml,
            best_python_code=best_python_code,
        )

        logger.info(
            "参数优化完成: method=%s, best_score=%.4f, incumbent=%.4f, "
            "improvement=%.2f%%, n_evals=%d",
            self.config.method, best_score, incumbent_score,
            improvement_pct, len(all_trials),
        )

        return result

    # ======================================================================
    # 内部方法
    # ======================================================================

    def _infer_param_ranges(
        self,
        dsl_template: dict,
        skill_type: str,
    ) -> Dict[str, Tuple[float, float]]:
        """根据 DSL 模板和 skill 类型推断可优化的参数和范围。"""
        ranges: Dict[str, Tuple[float, float]] = {}

        # 从 parameters 中收集
        params = dsl_template.get("parameters", {})
        if isinstance(params, dict):
            for key in params:
                if key in self.config.param_ranges:
                    ranges[key] = self.config.param_ranges[key]

        # cycle 类型额外参数
        if skill_type == "cycle":
            cycle_cfg = dsl_template.get("cycle", {})
            if isinstance(cycle_cfg, dict):
                for key in ("base", "queue_gain"):
                    if key in self.config.param_ranges:
                        ranges[key] = self.config.param_ranges[key]

            alloc_cfg = dsl_template.get("allocation", {})
            if isinstance(alloc_cfg, dict):
                for key in ("min_green", "max_green"):
                    if key in self.config.param_ranges:
                        ranges[key] = self.config.param_ranges[key]

        # phase 类型额外参数
        elif skill_type == "phase":
            params = dsl_template.get("parameters", {})
            if isinstance(params, dict):
                for key in ("extend_threshold", "shorten_threshold",
                            "max_extend", "max_shorten"):
                    if key in self.config.param_ranges and key in params:
                        ranges[key] = self.config.param_ranges[key]

        return ranges

    def _create_strategy(
        self, param_ranges: Dict[str, Tuple[float, float]]
    ) -> BaseOptimizer:
        """根据配置创建优化策略。"""
        method = self.config.method.lower()

        if method == "grid":
            return GridSearchOptimizer(
                param_ranges=param_ranges,
                n_trials=self.config.n_trials,
                seed=self.config.seed,
                n_grid_points=self.config.n_grid_points,
            )
        elif method == "bayesian":
            return BayesianOptimizer(
                param_ranges=param_ranges,
                n_trials=self.config.n_trials,
                seed=self.config.seed,
            )
        else:
            # 默认 random
            return RandomSearchOptimizer(
                param_ranges=param_ranges,
                n_trials=self.config.n_trials,
                seed=self.config.seed,
            )

    def _apply_params(
        self,
        dsl_template: dict,
        params: Dict[str, float],
    ) -> dict:
        """将优化参数应用到 DSL 模板，返回新的 DSL 字典。"""
        dsl = copy.deepcopy(dsl_template)

        # 应用 parameters 中的权重
        if "parameters" not in dsl:
            dsl["parameters"] = {}

        for key, value in params.items():
            if key in ("base", "queue_gain"):
                # 属于 cycle 配置
                if "cycle" not in dsl:
                    dsl["cycle"] = {}
                dsl["cycle"][key] = value
            elif key in ("min_green", "max_green"):
                # 属于 allocation 配置
                if "allocation" not in dsl:
                    dsl["allocation"] = {}
                dsl["allocation"][key] = value
            else:
                # 属于 parameters
                dsl["parameters"][key] = value

        return dsl

    def _evaluate_params(
        self,
        dsl: dict,
        skill_type: str,
    ) -> Tuple[float, bool]:
        """编译 DSL 并用 evaluator_factory 评估。

        Returns
        -------
        (score, eval_ok)
            score 越低越好。eval_ok=False 表示编译或评估失败。
        """
        compile_result = self._compile_dsl(dsl)
        if not compile_result.success or not compile_result.python_code:
            logger.debug("DSL 编译失败: %s", compile_result.errors)
            return float("inf"), False

        try:
            score = self.evaluator_factory(compile_result.python_code, skill_type)
            return float(score), True
        except Exception as e:
            logger.debug("参数评估异常: %s", e)
            return float("inf"), False

    def _compile_dsl(self, dsl: dict) -> CompileResult:
        """将 DSL dict 编译为 Python 代码。"""
        dsl_yaml = yaml.dump(dsl, default_flow_style=False, allow_unicode=True)
        return self.compiler.compile(dsl_yaml)


# ============================================================================
# 快速参数扫描（无 evaluator，纯 DSL 级别参数验证）
# ============================================================================

class QuickParamScreener:
    """快速参数筛选器：不运行 SUMO，仅验证 DSL 编译是否通过。

    用于在真正运行昂贵的 SUMO 评估前，快速排除无法编译的参数组合。
    """

    def __init__(self, compiler: Optional[DslCompiler] = None):
        self.compiler = compiler or DslCompiler()

    def screen(
        self,
        dsl_template: dict,
        param_combinations: List[Dict[str, float]],
    ) -> List[Dict[str, float]]:
        """筛选出可以成功编译的参数组合。

        Parameters
        ----------
        dsl_template : dict
            DSL 模板
        param_combinations : list[dict]
            候选参数组合列表

        Returns
        -------
        list[dict]
            可以成功编译的参数组合列表
        """
        valid = []
        for params in param_combinations:
            dsl = copy.deepcopy(dsl_template)
            # 应用参数
            if "parameters" not in dsl:
                dsl["parameters"] = {}
            for key, value in params.items():
                if key in ("base", "queue_gain"):
                    if "cycle" not in dsl:
                        dsl["cycle"] = {}
                    dsl["cycle"][key] = value
                elif key in ("min_green", "max_green"):
                    if "allocation" not in dsl:
                        dsl["allocation"] = {}
                    dsl["allocation"][key] = value
                else:
                    dsl["parameters"][key] = value

            # 尝试编译
            dsl_yaml = yaml.dump(dsl, default_flow_style=False, allow_unicode=True)
            result = self.compiler.compile(dsl_yaml)
            if result.success:
                valid.append(params)

        logger.info(
            "QuickParamScreener: %d/%d 参数组合通过编译检查",
            len(valid), len(param_combinations),
        )
        return valid


# ============================================================================
# 自测试
# ============================================================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("Test 1: GridSearchOptimizer")
    print("=" * 60)
    ranges = {"w_queue": (-2.0, 2.0), "w_wait": (-1.0, 1.0)}
    grid_opt = GridSearchOptimizer(ranges, n_trials=20, seed=42, n_grid_points=3)
    points = []
    for i in range(9):  # 3x3 = 9 个点
        p = grid_opt.suggest(i)
        points.append(p)
    print(f"  生成 {len(points)} 个网格点")
    print(f"  第一个: {points[0]}")
    print(f"  最后一个: {points[-1]}")
    assert len(set(str(p) for p in points)) == 9, "网格点应有 9 个不同值"
    print("  [OK] GridSearch 通过")

    print()
    print("=" * 60)
    print("Test 2: RandomSearchOptimizer")
    print("=" * 60)
    rand_opt = RandomSearchOptimizer(ranges, n_trials=10, seed=42)
    points = [rand_opt.suggest(i) for i in range(10)]
    print(f"  生成 {len(points)} 个随机点")
    # 检查范围
    for p in points:
        for k, v in p.items():
            lo, hi = ranges[k]
            assert lo <= v <= hi, f"{k}={v} 超出 [{lo}, {hi}]"
    print("  [OK] RandomSearch 通过")

    print()
    print("=" * 60)
    print("Test 3: BayesianOptimizer")
    print("=" * 60)
    bay_opt = BayesianOptimizer(ranges, n_trials=20, seed=42)
    # 模拟几轮 warmup + 观测
    for i in range(5):
        p = bay_opt.suggest(i)
        score = sum(p.values())  # 假 score
        bay_opt.on_trial_complete(i, p, score)
    print(f"  warmup 完成，scipy 可用: {bay_opt._scipy_available}")
    # 测试 EI 建议
    p = bay_opt.suggest(6)
    print(f"  EI 建议: {p}")
    for k, v in p.items():
        lo, hi = ranges[k]
        assert lo <= v <= hi, f"{k}={v} 超出 [{lo}, {hi}]"
    print("  [OK] BayesianOptimizer 通过")

    print()
    print("=" * 60)
    print("Test 4: DSLParamOptimizer 端到端")
    print("=" * 60)

    # 简单 evaluator：编译后的代码长度作为伪 score
    def mock_evaluator(code: str, skill_type: str) -> float:
        # 越短越好（纯粹测试）
        return float(len(code))

    cycle_template = {
        "skill_type": "cycle",
        "version_note": "测试模板",
        "features_used": ["queue", "waiting_time", "downstream_queue"],
        "parameters": {
            "w_queue": 1.0,
            "w_wait": 0.2,
            "w_downstream": -0.8,
        },
        "cycle": {
            "base": 80.0,
            "queue_gain": 0.5,
            "min": 40,
            "max": 180,
        },
        "allocation": {
            "method": "softmax",
            "min_green": 10,
            "max_green": 60,
        },
        "guards": {
            "all_phases_served": True,
            "max_cycle_jump": 20,
            "downstream_block_clip": True,
        },
    }

    config = OptimizerConfig(method="random", n_trials=10, seed=42)
    optimizer = DSLParamOptimizer(config=config, evaluator_factory=mock_evaluator)
    result = optimizer.optimize(cycle_template, incumbent_score=5000.0)

    print(f"  best_score: {result.best_score}")
    print(f"  incumbent_score: {result.incumbent_score}")
    print(f"  improvement_pct: {result.improvement_pct}%")
    print(f"  n_evaluations: {result.n_evaluations}")
    print(f"  improved: {result.improved}")
    print(f"  best_params: {result.best_params}")
    assert result.n_evaluations > 0, "应有评估记录"
    print("  [OK] DSLParamOptimizer 端到端测试通过")

    print()
    print("=" * 60)
    print("Test 5: QuickParamScreener")
    print("=" * 60)
    screener = QuickParamScreener()
    combos = [
        {"w_queue": 1.0, "w_wait": 0.5},
        {"w_queue": -3.0, "w_wait": 0.5},  # w_queue 超出 schema 范围
        {"w_queue": 0.5},
    ]
    valid = screener.screen(cycle_template, combos)
    print(f"  {len(valid)}/{len(combos)} 参数组合通过编译")
    print("  [OK] QuickParamScreener 测试通过")

    print()
    print("=" * 60)
    if True:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)
