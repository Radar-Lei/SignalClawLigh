"""ASTSandbox - AST 静态检查器，确保 GLM 生成的代码安全且符合规范。

检查内容包括：
- 禁止 import 危险模块
- 禁止调用危险函数
- 禁止属性访问危险对象
- 验证函数接口正确性
- 检查代码确定性（无随机数）
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from typing import List, Set


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ASTCheckResult:
    """AST 静态检查结果。"""
    passed: bool
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    has_correct_interface: bool = False
    complexity_score: float = 0.0


# ---------------------------------------------------------------------------
# ASTSandbox
# ---------------------------------------------------------------------------

class ASTSandbox:
    """AST 静态安全检查器。"""

    # 完全禁止的模块/名字
    FORBIDDEN_NAMES: Set[str] = {
        # IO & 文件
        "open", "exec", "eval", "compile", "__import__",
        # 系统 & 网络
        "os", "sys", "subprocess", "socket", "signal",
        "requests", "urllib", "http",
        # 随机 & 时间
        "random", "time", "datetime",
        # 进程 & 线程
        "threading", "multiprocessing",
        # 动态加载
        "importlib", "pkgutil", "code", "codeop",
        # 序列化
        "pickle", "shelve", "marshal", "json",
        # API 客户端
        "openai", "zhipuai", "dotenv",
        # 其他危险模块
        "ctypes", "shutil", "pathlib",
        "builtins",
    }

    # 允许的内置函数
    ALLOWED_BUILTINS: Set[str] = {
        "min", "max", "sum", "abs", "sorted", "len", "range", "enumerate",
        "float", "int", "dict", "list", "tuple", "set", "bool", "str",
        "isinstance", "round", "zip", "map", "filter", "any", "all",
        "reversed", "print", "hasattr", "getattr",
        "True", "False", "None",
    }

    # 允许的 import 模块
    ALLOWED_IMPORTS: Set[str] = {
        "math",
        "typing",
        "collections",
        "signalclaw",
    }

    # 禁止的函数调用名（作为直接调用）
    FORBIDDEN_CALLS: Set[str] = {
        "open", "exec", "eval", "compile", "__import__",
        "globals", "locals", "vars", "dir",
        "input", "breakpoint",
        "memoryview", "bytearray", "bytes",
        "type",  # 禁止动态创建类型
        "super",  # 禁止继承
    }

    def check(self, code: str, skill_type: str) -> ASTCheckResult:
        """对代码进行完整的 AST 静态安全检查。

        Parameters
        ----------
        code : str
            要检查的 Python 代码
        skill_type : str
            "cycle" 或 "phase"

        Returns
        -------
        ASTCheckResult
        """
        violations: List[str] = []
        warnings: List[str] = []

        # ---- Step 1: 解析 AST ----
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return ASTCheckResult(
                passed=False,
                violations=[f"语法错误: {e}"],
                warnings=[],
                has_correct_interface=False,
                complexity_score=0.0,
            )

        # ---- Step 2: 检查 import 语句 ----
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod_name = alias.name.split(".")[0]
                    if mod_name not in self.ALLOWED_IMPORTS:
                        violations.append(f"禁止 import: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mod_name = node.module.split(".")[0]
                    if mod_name not in self.ALLOWED_IMPORTS:
                        violations.append(f"禁止 from import: {node.module}")

        # ---- Step 3: 检查函数调用 ----
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._get_call_name(node)
                if func_name:
                    # 检查是否调用了禁止的函数
                    top_name = func_name.split(".")[0]
                    if top_name in self.FORBIDDEN_CALLS:
                        violations.append(f"禁止调用函数: {func_name}")
                    if top_name in self.FORBIDDEN_NAMES:
                        violations.append(f"禁止访问: {func_name}")

        # ---- Step 4: 检查属性访问 ----
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                attr_name = node.attr
                if attr_name.startswith("__") and attr_name.endswith("__"):
                    # 允许 __init__ 在函数定义外？不，我们禁止 class
                    if attr_name not in ("__name__",):
                        warnings.append(f"可疑的 dunder 属性访问: .{attr_name}")

        # ---- Step 5: 检查 class 定义 ----
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                violations.append("禁止使用 class 定义")

        # ---- Step 6: 检查全局变量赋值（允许模块级常量，禁止可变全局状态） ----
        _check_global_mutability(tree, violations, warnings)

        # ---- Step 7: 检查是否定义了正确的函数 ----
        has_correct_interface = self._check_interface(tree, skill_type, violations)

        # ---- Step 8: 检查确定性 ----
        determinism_ok = self.check_determinism(code)
        if not determinism_ok:
            violations.append("代码包含非确定性元素（随机数或时间调用）")

        # ---- Step 9: 计算复杂度 ----
        complexity = self._compute_complexity(tree)

        # ---- Step 10: 检查 try/except ----
        for node in ast.walk(tree):
            if isinstance(node, (ast.Try, ast.TryStar)):
                violations.append("禁止使用 try/except")

        passed = len(violations) == 0
        return ASTCheckResult(
            passed=passed,
            violations=violations,
            warnings=warnings,
            has_correct_interface=has_correct_interface,
            complexity_score=complexity,
        )

    def check_determinism(self, code: str) -> bool:
        """检查代码是否确定性（无随机数、无时间调用）。

        Returns
        -------
        bool
            True 表示代码看起来是确定性的
        """
        forbidden_determinism = {
            "random", "randint", "random", "uniform", "choice", "sample",
            "shuffle", "seed", "gauss", "normalvariate",
            "time", "sleep", "clock", "perf_counter", "monotonic",
            "datetime", "strftime", "strptime",
        }

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._get_call_name(node)
                if func_name:
                    top = func_name.split(".")[-1]  # 取最后一段
                    if top in forbidden_determinism:
                        return False
                    # 也检查完整名
                    if func_name in forbidden_determinism:
                        return False

            # 检查 import
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "random":
                        return False
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == "random":
                    return False

        return True

    # ======================================================================
    # Internal helpers
    # ======================================================================

    def _get_call_name(self, node: ast.Call) -> str:
        """从 Call 节点提取函数名的字符串表示。"""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            parts = []
            current = node.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            parts.reverse()
            return ".".join(parts)
        return ""

    def _check_interface(
        self, tree: ast.Module, skill_type: str, violations: List[str]
    ) -> bool:
        """检查是否定义了正确的函数接口。

        cycle skill: plan(obs) -> CyclePlan
        phase skill: decide(obs, plan) -> PhaseCommand
        """
        defined_functions = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                defined_functions.add(node.name)

        if skill_type == "cycle":
            if "plan" in defined_functions:
                # 检查参数数量
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "plan":
                        if len(node.args.args) != 1:
                            violations.append(
                                f"plan() 应该只接受 1 个参数 (obs)，实际有 {len(node.args.args)} 个"
                            )
                        break
                return True
            else:
                violations.append("cycle skill 必须定义 plan() 函数")
                return False

        elif skill_type == "phase":
            if "decide" in defined_functions:
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "decide":
                        if len(node.args.args) != 2:
                            violations.append(
                                f"decide() 应该接受 2 个参数 (obs, plan)，实际有 {len(node.args.args)} 个"
                            )
                        break
                return True
            else:
                violations.append("phase skill 必须定义 decide() 函数")
                return False

        violations.append(f"未知的 skill_type: {skill_type}")
        return False

    def _compute_complexity(self, tree: ast.Module) -> float:
        """计算代码复杂度评分（基于 McCabe 复杂度简化版）。

        评分越高，代码越复杂。
        """
        complexity = 1.0  # 基础复杂度

        for node in ast.walk(tree):
            # 分支增加复杂度
            if isinstance(node, (ast.If, ast.IfExp)):
                complexity += 1.0
            elif isinstance(node, ast.For):
                complexity += 1.0
            elif isinstance(node, ast.While):
                complexity += 2.0  # while 比 for 更危险
            elif isinstance(node, (ast.And, ast.Or)):
                complexity += 0.2
            # 嵌套调用
            elif isinstance(node, ast.Call):
                complexity += 0.1
            # 嵌套函数
            elif isinstance(node, ast.FunctionDef):
                complexity += 0.5
            # 字典推导/列表推导
            elif isinstance(node, (ast.DictComp, ast.ListComp, ast.SetComp)):
                complexity += 0.3

        return round(complexity, 2)


def _check_global_mutability(
    tree: ast.Module, violations: List[str], warnings: List[str]
) -> None:
    """检查全局变量是否为不可变类型（允许常量，禁止可变全局状态）。

    由于生成的 skill 代码可能使用模块级全局变量来保持状态，
    我们将其作为 warning 而非 violation。
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            # 检查是否是简单的不可变赋值
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    # 跳过以 _ 开头的私有变量（通常作为模块级状态）
                    if not name.startswith("_"):
                        # 检查赋值是否是可变类型
                        if isinstance(node.value, (ast.List, ast.Dict, ast.Set)):
                            warnings.append(
                                f"全局可变变量: {name}（可变类型）"
                            )
                    else:
                        # 私有全局变量
                        warnings.append(
                            f"模块级状态变量: {name}（可能导致不确定性）"
                        )
        elif isinstance(node, ast.AugAssign):
            # a += b 形式
            if isinstance(node.target, ast.Name):
                warnings.append(
                    f"全局变量增强赋值: {node.target.id}"
                )
