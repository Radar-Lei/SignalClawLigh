"""场景目录管理模块。

管理生成的仿真场景集合，支持目录的创建、保存、加载，
以及为每个场景生成 .sumocfg 文件。
"""

from __future__ import annotations

import json
import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from signalclaw.scenario.demand_generator import DemandGenerator, ScenarioConfig

logger = logging.getLogger(__name__)


@dataclass
class ScenarioEntry:
    """场景目录条目。"""

    name: str
    route_file: str  # 相对或绝对路径
    weight: float  # 多场景评估权重
    description: str
    sumocfg_file: str = ""  # 生成后填充


class ScenarioCatalog:
    """场景目录管理器。

    管理一组生成的仿真场景，支持：
    - 添加/删除场景条目
    - 保存/加载目录到 JSON 文件
    - 为每个场景生成 .sumocfg 文件
    - 创建基于 SQL 先验的默认场景集
    """

    def __init__(self):
        self.scenarios: List[ScenarioEntry] = []

    def add(self, entry: ScenarioEntry) -> None:
        """添加场景条目。"""
        self.scenarios.append(entry)
        logger.debug(f"场景目录添加: {entry.name} (weight={entry.weight})")

    def remove(self, name: str) -> bool:
        """按名称移除场景条目。

        Returns
        -------
        bool
            是否成功移除
        """
        for i, entry in enumerate(self.scenarios):
            if entry.name == name:
                self.scenarios.pop(i)
                return True
        return False

    def get(self, name: str) -> Optional[ScenarioEntry]:
        """按名称获取场景条目。"""
        for entry in self.scenarios:
            if entry.name == name:
                return entry
        return None

    def __len__(self) -> int:
        return len(self.scenarios)

    def __iter__(self):
        return iter(self.scenarios)

    # ======================================================================
    # 默认场景集
    # ======================================================================

    @classmethod
    def default_catalog(cls, output_dir: str) -> "ScenarioCatalog":
        """创建默认的场景目录（基于 SQL 先验的合理场景集）。

        场景设计参考 SQL 交通画像数据：
        - 早高峰 07:30-09:00，需求约 1.45x
        - 晚高峰 17:00-19:00，需求约 1.60x
        - 低需求时段约 0.35x
        - 主路/支路流量不均衡
        - 左转相位相关方向流量波动

        Parameters
        ----------
        output_dir : str
            场景文件输出目录

        Returns
        -------
        ScenarioCatalog
            包含默认场景配置的目录
        """
        catalog = cls()

        # 1. 基准场景 — 直接使用原始数据
        catalog.add(
            ScenarioEntry(
                name="base",
                route_file=os.path.join(output_dir, "chengdu_base.rou.xml"),
                weight=1.0,
                description="基准场景，使用原始需求模式",
            )
        )

        # 2. 早高峰场景
        catalog.add(
            ScenarioEntry(
                name="morning_peak",
                route_file=os.path.join(output_dir, "chengdu_morning_peak.rou.xml"),
                weight=1.2,
                description="早高峰场景，demand 1.45x，集中在前半段 900-1800s",
            )
        )

        # 3. 晚高峰场景
        catalog.add(
            ScenarioEntry(
                name="evening_peak",
                route_file=os.path.join(output_dir, "chengdu_evening_peak.rou.xml"),
                weight=1.2,
                description="晚高峰场景，demand 1.60x，集中在后半段 1800-3000s",
            )
        )

        # 4. 低需求场景
        catalog.add(
            ScenarioEntry(
                name="low_demand",
                route_file=os.path.join(output_dir, "chengdu_low_demand.rou.xml"),
                weight=0.8,
                description="低需求场景，demand 0.35x",
            )
        )

        # 5. 主路偏重场景
        catalog.add(
            ScenarioEntry(
                name="mainroad_imbalance",
                route_file=os.path.join(output_dir, "chengdu_mainroad_imbalance.rou.xml"),
                weight=1.0,
                description="主路偏重场景，主路方向 3x，支路 0.5x",
            )
        )

        # 6. 左转突增场景
        catalog.add(
            ScenarioEntry(
                name="leftturn_surge",
                route_file=os.path.join(output_dir, "chengdu_leftturn_surge.rou.xml"),
                weight=1.0,
                description="左转突增场景，左转相位相关方向 2.2x",
            )
        )

        # 7. 混合压力场景
        catalog.add(
            ScenarioEntry(
                name="mixed_stress",
                route_file=os.path.join(output_dir, "chengdu_mixed_stress.rou.xml"),
                weight=1.5,
                description="混合压力场景，1.3x + 方向不均衡 + 波动",
            )
        )

        return catalog

    # ======================================================================
    # 默认场景配置
    # ======================================================================

    @staticmethod
    def default_configs() -> List[ScenarioConfig]:
        """返回默认场景的配置列表。

        Returns
        -------
        list[ScenarioConfig]
            与 default_catalog 对应的场景配置
        """
        configs = [
            # 1. 基准场景
            ScenarioConfig(
                name="base",
                description="基准场景，使用原始需求模式",
                demand_multiplier=1.0,
                seed=42,
                weight=1.0,
            ),
            # 2. 早高峰（仿真时间 0-3600s 内的前半段高峰）
            ScenarioConfig(
                name="morning_peak",
                description="早高峰场景，demand 1.45x，集中在前半段 900-1800s",
                demand_multiplier=1.0,  # 基础不变
                peak_start=900.0,  # 仿真开始后 15 分钟
                peak_end=1800.0,  # 仿真开始后 30 分钟
                peak_multiplier=1.45,
                seed=42,
                weight=1.2,
            ),
            # 3. 晚高峰（仿真时间 0-3600s 内的后半段高峰）
            ScenarioConfig(
                name="evening_peak",
                description="晚高峰场景，demand 1.60x，集中在后半段 1800-3000s",
                demand_multiplier=1.0,
                peak_start=1800.0,  # 仿真 30 分钟处
                peak_end=3000.0,  # 仿真 50 分钟处
                peak_multiplier=1.60,
                seed=42,
                weight=1.2,
            ),
            # 4. 低需求
            ScenarioConfig(
                name="low_demand",
                description="低需求场景，demand 0.35x",
                demand_multiplier=0.35,
                seed=42,
                weight=0.8,
            ),
            # 5. 主路偏重
            ScenarioConfig(
                name="mainroad_imbalance",
                description="主路偏重场景，主路方向 3x，支路 0.5x",
                demand_multiplier=1.0,
                direction_bias={
                    # 主路 edge — 成都交叉口主干道（根据网络拓扑）
                    "-28621533#2": 3.0,
                    "28621533#2": 3.0,
                    "-28621533#1": 3.0,
                    "28621533#1": 3.0,
                    "-28621533#0": 3.0,
                    "28621533#0": 3.0,
                    # 支路 edge — 降低
                    "181190799": 0.5,
                    "-181190799": 0.5,
                },
                seed=42,
                weight=1.0,
            ),
            # 6. 左转突增
            ScenarioConfig(
                name="leftturn_surge",
                description="左转突增场景，左转相位相关方向 2.2x",
                demand_multiplier=1.0,
                direction_bias={
                    # 左转相关 edge（通常涉及跨越对向车流的路径）
                    "-463252780": 2.2,
                    "463252779": 2.2,
                    "-351972802": 2.2,
                    "351972802": 2.2,
                },
                seed=42,
                weight=1.0,
            ),
            # 7. 混合压力
            ScenarioConfig(
                name="mixed_stress",
                description="混合压力场景，1.3x + 方向不均衡 + 波动",
                demand_multiplier=1.3,
                peak_start=1200.0,  # 仿真 20 分钟处
                peak_end=2400.0,  # 仿真 40 分钟处
                peak_multiplier=1.5,
                direction_bias={
                    "-28621533#2": 1.8,
                    "28621533#2": 1.8,
                    "181190799": 0.6,
                },
                seed=42,
                weight=1.5,
            ),
        ]

        return configs

    # ======================================================================
    # 持久化
    # ======================================================================

    def save(self, path: str) -> None:
        """保存目录到 JSON 文件。

        Parameters
        ----------
        path : str
            输出 JSON 文件路径
        """
        data = {
            "version": "1.0",
            "scenarios": [asdict(e) for e in self.scenarios],
        }
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"场景目录已保存到 {path}，共 {len(self.scenarios)} 个场景")

    @classmethod
    def load(cls, path: str) -> "ScenarioCatalog":
        """从 JSON 文件加载目录。

        Parameters
        ----------
        path : str
            JSON 文件路径

        Returns
        -------
        ScenarioCatalog
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        catalog = cls()
        for s in data.get("scenarios", []):
            catalog.add(ScenarioEntry(**s))

        logger.info(f"场景目录从 {path} 加载完成，共 {len(catalog)} 个场景")
        return catalog

    # ======================================================================
    # .sumocfg 生成
    # ======================================================================

    def get_sumocfg_paths(self, net_file: str) -> List[str]:
        """为每个场景生成 .sumocfg 文件。

        所有场景共享同一个 .net.xml 文件。

        Parameters
        ----------
        net_file : str
            网络文件路径（.net.xml），会写入 .sumocfg 中的引用

        Returns
        -------
        list[str]
            生成的 .sumocfg 文件路径列表
        """
        paths = []

        for entry in self.scenarios:
            # .sumocfg 与 .rou.xml 同目录
            route_dir = os.path.dirname(entry.route_file)
            sumocfg_name = f"chengdu_{entry.name}.sumocfg"
            sumocfg_path = os.path.join(route_dir, sumocfg_name)

            self._generate_sumocfg(
                sumocfg_path=sumocfg_path,
                net_file=net_file,
                route_file=entry.route_file,
                scenario_name=entry.name,
            )

            entry.sumocfg_file = sumocfg_path
            paths.append(sumocfg_path)

        return paths

    @staticmethod
    def _generate_sumocfg(
        sumocfg_path: str,
        net_file: str,
        route_file: str,
        scenario_name: str,
        begin: float = 0,
        end: float = 3599.75,
    ) -> str:
        """生成单个 .sumocfg 文件。

        Parameters
        ----------
        sumocfg_path : str
            输出文件路径
        net_file : str
            网络文件路径（绝对路径或相对于 sumocfg 的相对路径）
        route_file : str
            路由文件路径
        scenario_name : str
            场景名称
        begin : float
            仿真开始时间
        end : float
            仿真结束时间
        """
        # 计算相对路径（sumocfg 引用 net 和 route 用相对路径）
        sumocfg_dir = os.path.dirname(sumocfg_path)

        # 网络文件相对于 sumocfg 的路径
        net_rel = os.path.relpath(net_file, sumocfg_dir)
        # 路由文件相对于 sumocfg 的路径
        route_rel = os.path.relpath(route_file, sumocfg_dir)

        root = ET.Element("configuration")

        # input 段
        input_elem = ET.SubElement(root, "input")
        ET.SubElement(input_elem, "net-file", value=net_rel)
        ET.SubElement(input_elem, "route-files", value=route_rel)

        # time 段
        time_elem = ET.SubElement(root, "time")
        ET.SubElement(time_elem, "begin", value=str(begin))
        ET.SubElement(time_elem, "end", value=str(end))

        # report 段（减少输出）
        report_elem = ET.SubElement(root, "report")
        ET.SubElement(report_elem, "no-warnings", value="true")
        ET.SubElement(report_elem, "no-step-log", value="true")

        tree = ET.ElementTree(root)
        ET.indent(tree, space="    ")

        os.makedirs(os.path.dirname(sumocfg_path) if os.path.dirname(sumocfg_path) else ".", exist_ok=True)
        with open(sumocfg_path, "wb") as f:
            tree.write(f, xml_declaration=True, encoding="utf-8")

        logger.debug(f".sumocfg 生成: {sumocfg_path}")
        return sumocfg_path

    # ======================================================================
    # 汇总信息
    # ======================================================================

    def summary(self) -> str:
        """返回目录的汇总描述。"""
        lines = [f"场景目录 ({len(self.scenarios)} 个场景):"]
        total_weight = sum(e.weight for e in self.scenarios)
        for entry in self.scenarios:
            pct = entry.weight / total_weight * 100 if total_weight > 0 else 0
            lines.append(
                f"  - {entry.name:20s} weight={entry.weight:.1f} ({pct:.1f}%) "
                f"| {entry.description}"
            )
        return "\n".join(lines)
