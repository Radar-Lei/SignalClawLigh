"""SUMO 场景生成脚本。

基于默认场景配置，批量生成仿真场景文件。

用法:
    python -m signalclaw.scripts.generate_scenarios
"""

from __future__ import annotations

import logging
import os
import sys

# 确保项目根目录在 sys.path 中
project_dir = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)

from signalclaw.scenario.demand_generator import DemandGenerator
from signalclaw.scenario.scenario_catalog import ScenarioCatalog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    """生成所有默认场景。"""
    # 路径配置
    net_file = os.path.join(
        project_dir, "sumo_scenarios", "chengdu", "chengdu.net.xml"
    )
    base_route = os.path.join(
        project_dir, "sumo_scenarios", "chengdu", "chengdu.rou.xml"
    )
    output_dir = os.path.join(
        project_dir, "sumo_scenarios", "chengdu", "generated"
    )
    catalog_path = os.path.join(output_dir, "scenario_catalog.json")

    # 检查输入文件
    if not os.path.exists(net_file):
        logger.error(f"网络文件不存在: {net_file}")
        sys.exit(1)
    if not os.path.exists(base_route):
        logger.error(f"基础路由文件不存在: {base_route}")
        sys.exit(1)

    logger.info(f"项目目录: {project_dir}")
    logger.info(f"网络文件: {net_file}")
    logger.info(f"基础路由: {base_route}")
    logger.info(f"输出目录: {output_dir}")

    # 创建生成器
    generator = DemandGenerator(net_file, base_route)

    # 获取默认配置
    configs = ScenarioCatalog.default_configs()
    logger.info(f"共 {len(configs)} 个场景待生成")

    # 批量生成场景 route 文件
    os.makedirs(output_dir, exist_ok=True)
    generated_paths = generator.generate_all(configs, output_dir)

    logger.info("=" * 60)
    logger.info("场景路由文件生成完成:")
    for path in generated_paths:
        size_mb = os.path.getsize(path) / (1024 * 1024)
        logger.info(f"  {os.path.basename(path):40s} {size_mb:.2f} MB")
    logger.info("=" * 60)

    # 创建场景目录
    catalog = ScenarioCatalog.default_catalog(output_dir)

    # 为每个场景生成 .sumocfg 文件
    sumocfg_paths = catalog.get_sumocfg_paths(net_file)
    logger.info("SUMO 配置文件生成完成:")
    for path in sumocfg_paths:
        logger.info(f"  {os.path.basename(path)}")

    # 保存目录
    catalog.save(catalog_path)
    logger.info(f"场景目录已保存: {catalog_path}")

    # 打印汇总
    print("\n" + "=" * 60)
    print(catalog.summary())
    print("=" * 60)

    # 打印基础统计
    print(f"\n基础车辆数: {len(generator._base_vehicles)}")
    for config in configs:
        entry = catalog.get(config.name)
        if entry:
            route_file = entry.route_file
            if os.path.exists(route_file):
                from signalclaw.scenario.demand_generator import DemandGenerator as DG

                temp_gen = DG.__new__(DG)
                temp_gen._base_vehicles = generator._parse_routes(route_file)
                stats = generator.get_stats(temp_gen._base_vehicles)
                print(
                    f"  {config.name:20s}: {stats['count']:6d} 辆, "
                    f"depart=[{stats['depart_min']:.1f}, {stats['depart_max']:.1f}]"
                )

    print("\n场景生成完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
