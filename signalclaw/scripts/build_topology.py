#!/usr/bin/env python3
"""Build one-hop neighbor topology from a SUMO network file.

Usage:
    python -m signalclaw.scripts.build_topology [--net NET_PATH] [--out OUT_PATH]

Defaults:
    --net  sumo_scenarios/chengdu/chengdu.net.xml
    --out  artifacts/topology/one_hop_neighbors.json
"""

from __future__ import annotations

import argparse
import os
import sys

# Ensure project root is importable when invoked directly
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from signalclaw.network.neighbor_graph import NeighborGraph


def main() -> None:
    parser = argparse.ArgumentParser(description="Build TLS neighbor topology")
    parser.add_argument(
        "--net",
        default=os.path.join(_PROJECT_ROOT, "sumo_scenarios", "chengdu", "chengdu.net.xml"),
        help="Path to SUMO .net.xml file",
    )
    parser.add_argument(
        "--out",
        default=os.path.join(_PROJECT_ROOT, "artifacts", "topology", "one_hop_neighbors.json"),
        help="Output JSON path",
    )
    args = parser.parse_args()

    print(f"Parsing SUMO network: {args.net}")
    graph = NeighborGraph.from_sumo_net(args.net)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    graph.save(args.out)

    stats = graph.stats()
    print(f"Saved topology to: {args.out}")
    print(f"  TLS count:            {stats['num_tls']}")
    print(f"  Upstream links:       {stats['total_upstream_links']}")
    print(f"  Downstream links:     {stats['total_downstream_links']}")
    print(f"  Avg upstream/TLS:     {stats['avg_upstream_per_tls']}")
    print(f"  Avg downstream/TLS:   {stats['avg_downstream_per_tls']}")


if __name__ == "__main__":
    main()
