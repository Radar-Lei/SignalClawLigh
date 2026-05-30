"""FeatureMask - Feature Availability Gating 机制。

确保 `predicted_arrival` 等未接入的特征在进化过程中不被候选 Skill 使用。
这防止 GLM 生成依赖不可用特征的代码，避免进化环境和执行环境不一致。

核心组件：
- DEFAULT_FEATURE_MASK: 默认的特征可用性配置
- FeatureMask: 特征门控管理器，提供检查、过滤、prompt 生成等功能
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set


# ============================================================================
# 默认特征可用性配置
# ============================================================================

DEFAULT_FEATURE_MASK: Dict[str, bool] = {
    "queue": True,
    "waiting_time": True,
    "downstream_queue": True,
    "neighbor_pressure": True,
    "predicted_arrival": False,  # 未接入，候选 Skill 不能使用
    "phase_elapsed": True,
    "phase_remaining": True,
    "hunger_time": True,
    "upstream_queue": True,
    "downstream_spillback_risk": True,
    "upstream_release_pressure": True,
    "neighbor_queue": True,
    "saturation_flow": True,
    "min_green": True,
    "max_green": True,
    "elapsed_green": True,
}

# PhaseObservation 字段名到 feature_mask 键的映射
_PHASE_OBS_FIELD_TO_FEATURE = {
    "queue": "queue",
    "waiting_time": "waiting_time",
    "predicted_arrival": "predicted_arrival",
    "elapsed_green": "elapsed_green",
    "min_green": "min_green",
    "max_green": "max_green",
    "saturation_flow": "saturation_flow",
}

# IntersectionObservation 字段名到 feature_mask 键的映射
_INTERSECTION_OBS_FIELD_TO_FEATURE = {
    "downstream_queue": "downstream_queue",
    "upstream_queue": "upstream_queue",
    "downstream_spillback_risk": "downstream_spillback_risk",
    "upstream_release_pressure": "upstream_release_pressure",
}

# DSL features_used 列表中的名称到 feature_mask 键的映射
_DSL_FEATURE_TO_MASK_KEY = {
    "queue": "queue",
    "waiting_time": "waiting_time",
    "predicted_arrival": "predicted_arrival",
    "downstream_queue": "downstream_queue",
    "hunger_time": "hunger_time",
    "upstream_queue": "upstream_queue",
    "downstream_spillback_risk": "downstream_spillback_risk",
    "upstream_release_pressure": "upstream_release_pressure",
    "neighbor_queue": "neighbor_queue",
}

# Phase DSL 评分项中使用的简称到 feature_mask 键的映射
_PHASE_DSL_SHORT_NAME_TO_FEATURE = {
    "arrival": "predicted_arrival",
    "hunger": "hunger_time",
    "downstream": "downstream_queue",
    "switch": None,  # switch 是内部逻辑，不是特征
}

# AST 代码中属性访问名到 feature_mask 键的映射
_AST_ATTR_TO_FEATURE = {
    "predicted_arrival": "predicted_arrival",
    "queue": "queue",
    "waiting_time": "waiting_time",
    "elapsed_green": "elapsed_green",
    "min_green": "min_green",
    "max_green": "max_green",
    "saturation_flow": "saturation_flow",
    "downstream_queue": "downstream_queue",
    "upstream_queue": "upstream_queue",
    "downstream_spillback_risk": "downstream_spillback_risk",
    "upstream_release_pressure": "upstream_release_pressure",
}


@dataclass
class FeatureMaskViolation:
    """单条特征门控违规。"""
    feature_name: str
    context: str  # "dsl" | "ast" | "parameter"
    message: str


@dataclass
class FeatureMaskCheckResult:
    """特征门控检查结果。"""
    passed: bool
    violations: List[FeatureMaskViolation] = field(default_factory=list)
    disabled_features_used: List[str] = field(default_factory=list)


class FeatureMask:
    """Feature Availability Gating 管理器。

    提供以下功能：
    1. 检查 DSL 是否使用了不可用特征
    2. 检查 AST 代码是否访问了不可用的字段
    3. 生成 GLM prompt 中可用/不可用特征的描述
    4. 过滤 DSL schema 中的不可用特征
    """

    def __init__(self, mask: Optional[Dict[str, bool]] = None):
        """初始化 FeatureMask。

        Parameters
        ----------
        mask : Dict[str, bool], optional
            特征可用性配置。为 None 时使用 DEFAULT_FEATURE_MASK。
        """
        self._mask = dict(mask or DEFAULT_FEATURE_MASK)

    @property
    def mask(self) -> Dict[str, bool]:
        """返回当前 feature mask 的副本。"""
        return dict(self._mask)

    def is_available(self, feature_name: str) -> bool:
        """检查指定特征是否可用。

        Parameters
        ----------
        feature_name : str
            特征名称（必须是 feature_mask 中定义的键）

        Returns
        -------
        bool
            True 表示可用，False 表示不可用。
            如果特征名不在 mask 中，默认返回 True。
        """
        return self._mask.get(feature_name, True)

    @property
    def available_features(self) -> FrozenSet[str]:
        """返回所有可用特征的冻结集合。"""
        return frozenset(k for k, v in self._mask.items() if v)

    @property
    def disabled_features(self) -> FrozenSet[str]:
        """返回所有不可用特征的冻结集合。"""
        return frozenset(k for k, v in self._mask.items() if not v)

    # ------------------------------------------------------------------
    # DSL 检查
    # ------------------------------------------------------------------

    def check_dsl(self, dsl_parsed: Dict[str, Any]) -> FeatureMaskCheckResult:
        """检查 DSL 是否使用了不可用的特征。

        检查内容：
        1. features_used 列表中的特征是否可用
        2. parameters 中与不可用特征相关的权重是否非零

        Parameters
        ----------
        dsl_parsed : Dict[str, Any]
            已解析的 DSL 字典

        Returns
        -------
        FeatureMaskCheckResult
        """
        violations: List[FeatureMaskViolation] = []
        disabled_used: List[str] = []

        # 检查 features_used
        features_used = dsl_parsed.get("features_used", [])
        for feat in features_used:
            mask_key = _DSL_FEATURE_TO_MASK_KEY.get(feat)
            if mask_key is not None and not self.is_available(mask_key):
                violations.append(FeatureMaskViolation(
                    feature_name=mask_key,
                    context="dsl",
                    message=(
                        f"DSL features_used 包含不可用特征 '{feat}' "
                        f"(feature_mask.{mask_key}=False)"
                    ),
                ))
                disabled_used.append(mask_key)

        # 检查 parameters 中与不可用特征相关的权重
        params = dsl_parsed.get("parameters", {})
        if isinstance(params, dict):
            # w_arrival 对应 predicted_arrival
            if not self.is_available("predicted_arrival"):
                w_arrival = params.get("w_arrival", 0.0)
                if w_arrival != 0.0:
                    violations.append(FeatureMaskViolation(
                        feature_name="predicted_arrival",
                        context="parameter",
                        message=(
                            f"DSL parameters.w_arrival={w_arrival} 非零，"
                            f"但 predicted_arrival 未接入 (feature_mask.predicted_arrival=False)"
                        ),
                    ))
                    if "predicted_arrival" not in disabled_used:
                        disabled_used.append("predicted_arrival")

        return FeatureMaskCheckResult(
            passed=len(violations) == 0,
            violations=violations,
            disabled_features_used=disabled_used,
        )

    # ------------------------------------------------------------------
    # AST 代码检查
    # ------------------------------------------------------------------

    def check_ast_code(self, code: str) -> FeatureMaskCheckResult:
        """检查 Python 代码 AST 是否访问了不可用的特征字段。

        主要检查属性访问模式：
        - phase_obs.predicted_arrival
        - current_phase_obs.predicted_arrival
        - xxx.predicted_arrival

        Parameters
        ----------
        code : str
            Python 代码字符串

        Returns
        -------
        FeatureMaskCheckResult
        """
        import ast as _ast

        violations: List[FeatureMaskViolation] = []
        disabled_used: List[str] = []

        try:
            tree = _ast.parse(code)
        except SyntaxError:
            return FeatureMaskCheckResult(
                passed=True,  # 语法错误由 AST sandbox 处理
                violations=[],
                disabled_features_used=[],
            )

        # 检查属性访问
        disabled = self.disabled_features
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Attribute):
                attr_name = node.attr
                # 检查属性名是否对应不可用的特征
                for disabled_feat in disabled:
                    # 直接匹配属性名（如 predicted_arrival）
                    if attr_name == disabled_feat:
                        # 确认这是在访问观测数据字段，而非其他变量
                        self._check_attribute_context(
                            node, disabled_feat, violations, disabled_used,
                        )
                        break
                    # 匹配 IntersectionObservation 级别的属性
                    if attr_name in _INTERSECTION_OBS_FIELD_TO_FEATURE:
                        feat_key = _INTERSECTION_OBS_FIELD_TO_FEATURE[attr_name]
                        if feat_key in disabled and feat_key not in disabled_used:
                            violations.append(FeatureMaskViolation(
                                feature_name=feat_key,
                                context="ast",
                                message=(
                                    f"代码访问了不可用的属性 '{attr_name}' "
                                    f"(feature_mask.{feat_key}=False)"
                                ),
                            ))
                            disabled_used.append(feat_key)

        return FeatureMaskCheckResult(
            passed=len(violations) == 0,
            violations=violations,
            disabled_features_used=list(dict.fromkeys(disabled_used)),  # 去重保序
        )

    def _check_attribute_context(
        self,
        node,
        feature_name: str,
        violations: List[FeatureMaskViolation],
        disabled_used: List[str],
    ) -> None:
        """检查属性访问的上下文，确认是否在访问观测数据字段。"""
        import ast as _ast

        # 获取被访问的对象名
        if isinstance(node.value, _ast.Name):
            obj_name = node.value.id
        elif isinstance(node.value, _ast.Attribute):
            # 嵌套属性访问，如 obs.ego.phases[0].predicted_arrival
            obj_name = self._get_root_name(node.value)
        else:
            obj_name = ""

        # 常见的观测数据变量名模式
        obs_patterns = {
            "phase_obs", "current_phase_obs", "p", "po",
            "obs", "ego", "neighbor_obs", "nobs",
        }

        # 如果属性名直接匹配不可用特征，且上下文看起来像观测数据访问
        # 由于我们无法完全确定语义，保守地对 predicted_arrival 做严格检查
        if feature_name == "predicted_arrival":
            if feature_name not in disabled_used:
                violations.append(FeatureMaskViolation(
                    feature_name=feature_name,
                    context="ast",
                    message=(
                        f"代码访问了 'predicted_arrival' 属性，"
                        f"但该特征未接入 (feature_mask.predicted_arrival=False)。"
                        f"请移除所有对 predicted_arrival 的引用。"
                    ),
                ))
                disabled_used.append(feature_name)

    @staticmethod
    def _get_root_name(node) -> str:
        """获取 AST 属性链的根变量名。"""
        import ast as _ast
        current = node
        while isinstance(current, _ast.Attribute):
            current = current.value
        if isinstance(current, _ast.Name):
            return current.id
        return ""

    # ------------------------------------------------------------------
    # Prompt 生成
    # ------------------------------------------------------------------

    def get_available_features_description(self) -> str:
        """生成可用特征描述（供 GLM prompt 使用）。

        Returns
        -------
        str
            格式化的可用/不可用特征描述
        """
        lines = ["## 特征可用性（Feature Availability）"]

        available = sorted(self.available_features)
        if available:
            lines.append("可用特征（你可以在代码中使用）:")
            for feat in available:
                lines.append(f"  - {feat}")
        else:
            lines.append("（无可用特征）")

        disabled = sorted(self.disabled_features)
        if disabled:
            lines.append("")
            lines.append("**不可用特征（禁止使用，违反将被拒绝）**:")
            for feat in disabled:
                lines.append(f"  - {feat}")
            lines.append("")
            lines.append(
                "重要：你的代码中不能出现上述不可用特征的任何引用，"
                "包括属性访问（如 .predicted_arrival）、变量名、"
                "参数权重（如 w_arrival）等。"
            )

        return "\n".join(lines)

    def get_disabled_feature_names(self) -> List[str]:
        """返回不可用特征的名称列表（用于 prompt 注入）。"""
        return sorted(self.disabled_features)

    def filter_dsl_features_used(self, features: List[str]) -> List[str]:
        """过滤 DSL features_used 列表，移除不可用的特征。

        Parameters
        ----------
        features : List[str]
            原始 features_used 列表

        Returns
        -------
        List[str]
            过滤后的列表（仅保留可用特征）
        """
        result = []
        for feat in features:
            mask_key = _DSL_FEATURE_TO_MASK_KEY.get(feat)
            if mask_key is None or self.is_available(mask_key):
                result.append(feat)
        return result

    def filter_dsl_parameters(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """过滤 DSL parameters，移除与不可用特征相关的权重。

        Parameters
        ----------
        params : Dict[str, Any]
            原始参数字典

        Returns
        -------
        Dict[str, Any]
            过滤后的参数字典
        """
        result = dict(params)

        if not self.is_available("predicted_arrival"):
            result.pop("w_arrival", None)

        return result

    def filter_json_schema_features(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        """过滤 JSON Schema 中的特征 enum，移除不可用特征。

        Parameters
        ----------
        schema : Dict[str, Any]
            原始 JSON schema

        Returns
        -------
        Dict[str, Any]
            过滤后的 schema（深拷贝）
        """
        import copy
        result = copy.deepcopy(schema)

        # 过滤 features_used 的 enum
        disabled = self.disabled_features
        _filter_schema_enum(result, disabled)
        return result


def _filter_schema_enum(schema: Any, disabled: FrozenSet[str]) -> None:
    """递归过滤 JSON Schema 中的 enum 值。"""
    if isinstance(schema, dict):
        if "enum" in schema and isinstance(schema["enum"], list):
            schema["enum"] = [
                v for v in schema["enum"]
                if v not in disabled
            ]
        for value in schema.values():
            _filter_schema_enum(value, disabled)
    elif isinstance(schema, list):
        for item in schema:
            _filter_schema_enum(item, disabled)
