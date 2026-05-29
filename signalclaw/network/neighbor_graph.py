"""
One-hop neighbor topology for SUMO traffic light networks.

Parses a SUMO .net.xml file to build a graph of TLS neighbors connected
by road edges. For each TLS we record upstream and downstream neighbors
with the connecting edges and estimated free-flow travel time.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class NeighborInfo:
    """One neighbor relation between two TLS."""

    neighbor_tls_id: str
    from_edge: str          # edge departing from the source TLS
    to_edge: str            # edge arriving at the target TLS
    travel_time_s: float    # estimated free-flow travel time (seconds)
    distance_m: float = 0.0
    movement_mapping: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "neighbor_tls_id": self.neighbor_tls_id,
            "from_edge": self.from_edge,
            "to_edge": self.to_edge,
            "travel_time_s": self.travel_time_s,
            "distance_m": self.distance_m,
            "movement_mapping": self.movement_mapping,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NeighborInfo":
        return cls(
            neighbor_tls_id=d["neighbor_tls_id"],
            from_edge=d["from_edge"],
            to_edge=d["to_edge"],
            travel_time_s=d["travel_time_s"],
            distance_m=d.get("distance_m", 0.0),
            movement_mapping=d.get("movement_mapping", {}),
        )


class NeighborGraph:
    """One-hop neighbor topology extracted from a SUMO network."""

    DEFAULT_TRAVEL_TIME = 30.0  # fallback when edge speed / length unavailable
    MAX_HOPS = 10               # maximum BFS depth when tracing edges

    def __init__(self):
        self.upstream: Dict[str, List[NeighborInfo]] = defaultdict(list)
        self.downstream: Dict[str, List[NeighborInfo]] = defaultdict(list)
        # convenience: set of all TLS IDs in this graph
        self.tls_ids: set = set()

    # ------------------------------------------------------------------
    # Construction from SUMO net.xml
    # ------------------------------------------------------------------

    @classmethod
    def from_sumo_net(cls, net_path: str) -> "NeighborGraph":
        """Parse one-hop neighbor topology from a SUMO .net.xml file."""
        import sumolib

        graph = cls()
        net = sumolib.net.readNet(net_path, withConnections=True, withInternal=True)

        tls_list = net.getTrafficLights()
        tls_node_ids: set = set()
        tls_map: Dict[str, object] = {}

        for tls in tls_list:
            tid = tls.getID()
            tls_node_ids.add(tid)
            tls_map[tid] = tls

        graph.tls_ids = set(tls_node_ids)

        for tls in tls_list:
            tls_id = tls.getID()
            tls_edge_ids = {e.getID() for e in tls.getEdges() if not e.isSpecial()}

            # ---------- downstream ----------
            graph.downstream[tls_id] = _find_downstream(
                tls_id, net, tls_node_ids, tls_edge_ids, cls.MAX_HOPS
            )

            # ---------- upstream ----------
            graph.upstream[tls_id] = _find_upstream(
                tls, tls_id, net, tls_node_ids, tls_edge_ids, cls.MAX_HOPS
            )

        return graph

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_one_hop_neighbors(self, tls_id: str) -> Dict[str, List[NeighborInfo]]:
        """Return upstream and downstream neighbor lists for a TLS."""
        return {
            "upstream": self.upstream.get(tls_id, []),
            "downstream": self.downstream.get(tls_id, []),
        }

    def get_neighbor_tls_ids(self, tls_id: str) -> set:
        """Return the set of unique TLS IDs that are one-hop neighbors."""
        ids: set = set()
        for nb in self.upstream.get(tls_id, []):
            ids.add(nb.neighbor_tls_id)
        for nb in self.downstream.get(tls_id, []):
            ids.add(nb.neighbor_tls_id)
        return ids

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "tls_ids": sorted(self.tls_ids),
            "upstream": {
                tid: [nb.to_dict() for nb in nbs]
                for tid, nbs in self.upstream.items()
            },
            "downstream": {
                tid: [nb.to_dict() for nb in nbs]
                for tid, nbs in self.downstream.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NeighborGraph":
        graph = cls()
        graph.tls_ids = set(d.get("tls_ids", []))
        for tid, nbs in d.get("upstream", {}).items():
            graph.upstream[tid] = [NeighborInfo.from_dict(nb) for nb in nbs]
        for tid, nbs in d.get("downstream", {}).items():
            graph.downstream[tid] = [NeighborInfo.from_dict(nb) for nb in nbs]
        return graph

    def save(self, path: str):
        """Serialize to a JSON file."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "NeighborGraph":
        """Load from a JSON file produced by :meth:`save`."""
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def stats(self) -> dict:
        """Quick summary statistics."""
        total_us = sum(len(v) for v in self.upstream.values())
        total_ds = sum(len(v) for v in self.downstream.values())
        return {
            "num_tls": len(self.tls_ids),
            "total_upstream_links": total_us,
            "total_downstream_links": total_ds,
            "avg_upstream_per_tls": round(total_us / max(len(self.tls_ids), 1), 2),
            "avg_downstream_per_tls": round(total_ds / max(len(self.tls_ids), 1), 2),
        }


# ======================================================================
# Internal helpers (module-level for testability)
# ======================================================================

def _edge_travel_time(edge) -> float:
    """Free-flow travel time along a single edge (seconds)."""
    speed = edge.getSpeed()
    length = edge.getLength()
    if speed and speed > 0 and length and length > 0:
        return length / speed
    return 0.0


def _find_downstream(
    tls_id: str,
    net,
    tls_node_ids: set,
    tls_edge_ids: set,
    max_hops: int,
) -> List[NeighborInfo]:
    """BFS from a TLS node along outgoing edges to find downstream TLS neighbors."""
    results: List[NeighborInfo] = []
    tls_node = net.getNode(tls_id)
    visited: set = set()

    for start_edge in tls_node.getOutgoing():
        if start_edge.isSpecial():
            continue
        queue = [(start_edge, 1, start_edge.getLength(), _edge_travel_time(start_edge))]
        visited.add(start_edge.getID())

        while queue:
            edge, depth, acc_len, acc_tt = queue.pop(0)
            to_node = edge.getToNode()

            if to_node.getID() in tls_node_ids and to_node.getID() != tls_id:
                results.append(NeighborInfo(
                    neighbor_tls_id=to_node.getID(),
                    from_edge=start_edge.getID(),
                    to_edge=edge.getID(),
                    travel_time_s=round(acc_tt, 2),
                    distance_m=round(acc_len, 2),
                ))
                continue  # stop at first TLS encountered

            if depth >= max_hops:
                continue

            for nxt in to_node.getOutgoing():
                if nxt.isSpecial() or nxt.getID() in visited:
                    continue
                visited.add(nxt.getID())
                queue.append((
                    nxt, depth + 1,
                    acc_len + nxt.getLength(),
                    acc_tt + _edge_travel_time(nxt),
                ))

    return results


def _find_upstream(
    tls,
    tls_id: str,
    net,
    tls_node_ids: set,
    tls_edge_ids: set,
    max_hops: int,
) -> List[NeighborInfo]:
    """BFS backwards from TLS-controlled edges to find upstream TLS neighbors."""
    results: List[NeighborInfo] = []
    visited: set = set()

    for tls_edge in tls.getEdges():
        if tls_edge.isSpecial():
            continue
        from_node = tls_edge.getFromNode()

        for prev_edge in from_node.getIncoming():
            if prev_edge.isSpecial() or prev_edge.getID() in tls_edge_ids:
                continue
            queue = [(
                prev_edge, 1,
                prev_edge.getLength(),
                _edge_travel_time(prev_edge),
            )]
            visited.add(prev_edge.getID())

            while queue:
                edge, depth, acc_len, acc_tt = queue.pop(0)
                src_node = edge.getFromNode()

                if src_node.getID() in tls_node_ids and src_node.getID() != tls_id:
                    results.append(NeighborInfo(
                        neighbor_tls_id=src_node.getID(),
                        from_edge=edge.getID(),
                        to_edge=tls_edge.getID(),
                        travel_time_s=round(acc_tt, 2),
                        distance_m=round(acc_len, 2),
                    ))
                    continue

                if depth >= max_hops:
                    continue

                for pe in src_node.getIncoming():
                    if pe.isSpecial() or pe.getID() in visited or pe.getID() in tls_edge_ids:
                        continue
                    visited.add(pe.getID())
                    queue.append((
                        pe, depth + 1,
                        acc_len + pe.getLength(),
                        acc_tt + _edge_travel_time(pe),
                    ))

    return results
