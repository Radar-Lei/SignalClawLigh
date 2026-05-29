"""SUMO 需求场景生成器。

基于 SQL 参考画像，通过修改原始 route 文件中的车辆 depart 时间、
删除/复制车辆来构造不同 demand 的仿真场景。
"""

from __future__ import annotations

import copy
import logging
import math
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ScenarioConfig:
    """场景配置。"""

    name: str
    description: str
    demand_multiplier: float = 1.0  # 总需求倍率
    peak_start: Optional[float] = None  # 高峰开始时间（秒）
    peak_end: Optional[float] = None  # 高峰结束时间（秒）
    peak_multiplier: float = 1.0  # 高峰期额外倍率
    direction_bias: Dict[str, float] = field(default_factory=dict)  # edge_id -> 倍率
    seed: int = 42
    weight: float = 1.0  # 在多场景评估中的权重

    @property
    def effective_demand(self) -> float:
        """综合考虑基础倍率和高峰倍率的有效需求倍率。"""
        return self.demand_multiplier


class DemandGenerator:
    """基于 SQL 参考画像的 SUMO 需求场景生成器。

    读取原始 route 文件，然后通过修改 depart 时间、
    删除/复制车辆来构造不同 demand 场景。

    核心策略：
    - demand_multiplier > 1: 复制部分车辆（增加需求）
    - demand_multiplier < 1: 删除部分车辆（减少需求）
    - peak_pattern: 在指定时段内额外增加需求
    - direction_bias: 对经过特定 edge 的车辆调整数量
    """

    def __init__(self, net_file: str, base_route_file: str):
        """
        Parameters
        ----------
        net_file : str
            SUMO 网络文件路径（.net.xml），用于获取 edge 信息
        base_route_file : str
            原始车辆路由文件路径（.rou.xml）
        """
        self.net_file = net_file
        self.base_route_file = base_route_file
        self._base_vehicles = self._parse_routes(base_route_file)
        logger.info(
            f"DemandGenerator 初始化完成，加载了 {len(self._base_vehicles)} 辆基础车辆"
        )

    # ======================================================================
    # 路由文件解析
    # ======================================================================

    def _parse_routes(self, route_file: str) -> List[dict]:
        """解析原始 route 文件，返回车辆列表。

        Returns
        -------
        list[dict]
            每个元素包含：
            - id: 车辆 ID
            - depart: 出发时间（秒）
            - edges: 路径 edge 列表
            - attrs: 其他属性（departLane, departPos, color 等）
        """
        tree = ET.parse(route_file)
        root = tree.getroot()

        vehicles = []
        for veh_elem in root.iter("vehicle"):
            route_elem = veh_elem.find("route")
            if route_elem is None:
                continue

            edges_str = route_elem.get("edges", "")
            edges = edges_str.split() if edges_str.strip() else []

            attrs = {}
            for attr_name in veh_elem.attrib:
                if attr_name not in ("id", "depart"):
                    attrs[attr_name] = veh_elem.get(attr_name)

            vehicles.append(
                {
                    "id": veh_elem.get("id", ""),
                    "depart": float(veh_elem.get("depart", "0.0")),
                    "edges": edges,
                    "attrs": attrs,
                }
            )

        # 按 depart 时间排序
        vehicles.sort(key=lambda v: v["depart"])
        return vehicles

    # ======================================================================
    # 场景生成
    # ======================================================================

    def generate(self, config: ScenarioConfig, output_path: str) -> str:
        """根据配置生成场景 route 文件。

        Parameters
        ----------
        config : ScenarioConfig
            场景配置
        output_path : str
            输出文件路径

        Returns
        -------
        str
            生成的文件路径
        """
        rng = __import__("random").Random(config.seed)

        # 1. 从基础车辆开始
        vehicles = copy.deepcopy(self._base_vehicles)

        # 2. 应用基础需求倍率
        vehicles = self._apply_demand_multiplier(
            vehicles, config.demand_multiplier, rng
        )

        # 3. 应用高峰模式
        if config.peak_start is not None and config.peak_end is not None:
            vehicles = self._apply_peak_pattern(
                vehicles,
                config.peak_start,
                config.peak_end,
                config.peak_multiplier,
                config.demand_multiplier,
                rng,
            )

        # 4. 应用方向偏好
        if config.direction_bias:
            vehicles = self._apply_direction_bias(
                vehicles, config.direction_bias, rng
            )

        # 5. 重新分配车辆 ID（避免冲突）
        vehicles = self._reassign_ids(vehicles, config.name)

        # 6. 按 depart 排序
        vehicles.sort(key=lambda v: v["depart"])

        # 7. 写入文件
        self._write_routes(vehicles, output_path)

        logger.info(
            f"场景 '{config.name}' 生成完成: {len(vehicles)} 辆车 -> {output_path}"
        )
        return output_path

    def generate_all(
        self, configs: List[ScenarioConfig], output_dir: str
    ) -> List[str]:
        """批量生成所有场景。

        Parameters
        ----------
        configs : list[ScenarioConfig]
            场景配置列表
        output_dir : str
            输出目录

        Returns
        -------
        list[str]
            生成的文件路径列表
        """
        os.makedirs(output_dir, exist_ok=True)
        paths = []

        for config in configs:
            filename = f"chengdu_{config.name}.rou.xml"
            output_path = os.path.join(output_dir, filename)
            self.generate(config, output_path)
            paths.append(output_path)

        return paths

    # ======================================================================
    # 需求调整策略
    # ======================================================================

    def _apply_demand_multiplier(
        self,
        vehicles: List[dict],
        multiplier: float,
        rng: __import__("random").Random,
    ) -> List[dict]:
        """调整总需求量：随机删除或复制车辆。

        multiplier > 1: 随机复制部分车辆（增加需求）
        multiplier < 1: 随机删除部分车辆（减少需求）
        multiplier == 1: 不变
        """
        if abs(multiplier - 1.0) < 0.01:
            return vehicles

        result = list(vehicles)

        if multiplier < 1.0:
            # 删除车辆以降低需求
            keep_ratio = multiplier
            n_keep = max(1, int(len(result) * keep_ratio))
            result = rng.sample(result, n_keep)
        else:
            # 复制车辆以增加需求
            n_copies = int(len(result) * (multiplier - 1.0))
            if n_copies > 0:
                copies = []
                for _ in range(n_copies):
                    source = rng.choice(vehicles)
                    copy_veh = copy.deepcopy(source)
                    # 添加微小随机偏移到 depart 时间
                    copy_veh["depart"] += rng.uniform(0.0, 5.0)
                    copies.append(copy_veh)
                result.extend(copies)

        return result

    def _apply_peak_pattern(
        self,
        vehicles: List[dict],
        peak_start: float,
        peak_end: float,
        peak_multiplier: float,
        base_multiplier: float,
        rng: __import__("random").Random,
    ) -> List[dict]:
        """在高峰时段增加需求。

        对 depart 在高峰时段内的车辆，额外复制一部分，
        并添加微小时间偏移。
        """
        peak_duration = peak_end - peak_start
        if peak_duration <= 0:
            return vehicles

        # 找出高峰时段内的车辆
        peak_vehicles = [
            v for v in vehicles if peak_start <= v["depart"] < peak_end
        ]

        if not peak_vehicles:
            return vehicles

        # 计算额外需要复制的数量
        extra_ratio = peak_multiplier - base_multiplier
        if extra_ratio <= 0:
            return vehicles

        n_extra = int(len(peak_vehicles) * extra_ratio)
        if n_extra <= 0:
            return vehicles

        # 随机选择车辆复制
        copies = []
        for _ in range(n_extra):
            source = rng.choice(peak_vehicles)
            copy_veh = copy.deepcopy(source)
            # 在高峰时段内随机偏移 depart 时间
            copy_veh["depart"] += rng.uniform(0.0, min(30.0, peak_duration * 0.1))
            copies.append(copy_veh)

        result = list(vehicles)
        result.extend(copies)
        return result

    def _apply_direction_bias(
        self,
        vehicles: List[dict],
        direction_bias: Dict[str, float],
        rng: __import__("random").Random,
    ) -> List[dict]:
        """对特定方向的车辆调整数量。

        direction_bias: edge_id -> 倍率
        如果某车辆的路径包含指定 edge，则按对应倍率调整。
        """
        if not direction_bias:
            return vehicles

        result = []
        for veh in vehicles:
            result.append(veh)

            # 检查车辆是否经过任何偏好 edge
            for edge_id, bias in direction_bias.items():
                if self._vehicle_uses_edge(veh, edge_id):
                    # 根据倍率决定是否额外复制
                    if bias > 1.0:
                        n_extra = int(bias) - 1
                        for _ in range(n_extra):
                            copy_veh = copy.deepcopy(veh)
                            copy_veh["depart"] += rng.uniform(0.0, 10.0)
                            result.append(copy_veh)
                    elif bias < 1.0:
                        # 按概率删除
                        if rng.random() > bias:
                            if veh in result:
                                result.remove(veh)
                    break  # 一个车辆只应用一次方向偏好

        return result

    @staticmethod
    def _vehicle_uses_edge(vehicle: dict, edge_id: str) -> bool:
        """检查车辆路径是否包含指定 edge。"""
        for e in vehicle["edges"]:
            # 支持前缀匹配（edge ID 可能包含 # 等后缀）
            if e == edge_id or e.startswith(edge_id):
                return True
        return False

    # ======================================================================
    # ID 重分配与文件写入
    # ======================================================================

    @staticmethod
    def _reassign_ids(vehicles: List[dict], scenario_name: str) -> List[dict]:
        """重新分配车辆 ID，避免冲突。

        格式: {scenario_name}_{序号}
        """
        seen_ids = set()
        counter = 0

        for veh in vehicles:
            while True:
                new_id = f"{scenario_name}_{counter}"
                if new_id not in seen_ids:
                    break
                counter += 1
            veh["id"] = new_id
            seen_ids.add(new_id)
            counter += 1

        return vehicles

    def _write_routes(self, vehicles: List[dict], output_path: str) -> None:
        """将车辆列表写入 SUMO route XML 文件。

        Parameters
        ----------
        vehicles : list[dict]
            车辆列表
        output_path : str
            输出文件路径
        """
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        root = ET.Element("routes")
        root.set(
            "xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance"
        )
        root.set(
            "xsi:noNamespaceSchemaLocation",
            "http://sumo.dlr.de/xsd/routes_file.xsd",
        )

        for veh in vehicles:
            veh_elem = ET.SubElement(root, "vehicle")
            veh_elem.set("id", veh["id"])
            veh_elem.set("depart", f"{veh['depart']:.2f}")

            # 写入额外属性
            for attr_name, attr_value in veh["attrs"].items():
                veh_elem.set(attr_name, attr_value)

            route_elem = ET.SubElement(veh_elem, "route")
            route_elem.set("edges", " ".join(veh["edges"]))

        tree = ET.ElementTree(root)
        ET.indent(tree, space="    ")

        with open(output_path, "wb") as f:
            tree.write(f, xml_declaration=True, encoding="utf-8")

    # ======================================================================
    # 统计信息
    # ======================================================================

    def get_stats(self, vehicles: List[dict]) -> Dict[str, any]:
        """获取车辆列表的统计信息。"""
        if not vehicles:
            return {"count": 0}

        departs = [v["depart"] for v in vehicles]
        return {
            "count": len(vehicles),
            "depart_min": min(departs),
            "depart_max": max(departs),
            "depart_mean": sum(departs) / len(departs),
            "unique_routes": len(
                set(" ".join(v["edges"]) for v in vehicles)
            ),
        }
