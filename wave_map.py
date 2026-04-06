"""
WAVE Fleet Planner — 맵 그래프 로드/저장 및 A* 경로 계획.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from heapq import heappop, heappush
from typing import Any, Dict, List, Optional, Set, Tuple

Vec2 = Tuple[float, float]


def _default_map_dict() -> Dict[str, Any]:
    """map.json이 존재하면 그 내용을 반환, 없으면 최소 빈 맵 반환."""
    _here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.json")
    if os.path.isfile(_here):
        try:
            with open(_here, "r", encoding="utf-8") as _f:
                return json.load(_f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"version": 1, "name": "fab_l1_default", "imageDataUrl": "", "imageWidth": 0, "imageHeight": 0, "nodes": [], "edges": []}


def _euclid(a: Vec2, b: Vec2) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _build_adjacency(nodes_by_id: Dict[str, Dict[str, Any]], edges: List[Dict[str, str]]) -> Dict[str, List[Tuple[str, float]]]:
    adj: Dict[str, List[Tuple[str, float]]] = {nid: [] for nid in nodes_by_id}
    for e in edges:
        a, b = e.get("from"), e.get("to")
        if not a or not b or a not in nodes_by_id or b not in nodes_by_id:
            continue
        pa = (float(nodes_by_id[a]["x"]), float(nodes_by_id[a]["y"]))
        pb = (float(nodes_by_id[b]["x"]), float(nodes_by_id[b]["y"]))
        w = _euclid(pa, pb)
        adj[a].append((b, w))
        adj[b].append((a, w))
    return adj


def astar(
    adj: Dict[str, List[Tuple[str, float]]],
    nodes_xy: Dict[str, Vec2],
    start: str,
    goal: str,
) -> Optional[List[str]]:
    if start == goal:
        return [start]
    if start not in adj or goal not in adj:
        return None

    def h(nid: str) -> float:
        return _euclid(nodes_xy[nid], nodes_xy[goal])

    open_heap: List[Tuple[float, str]] = []
    heappush(open_heap, (h(start), start))
    came: Dict[str, Optional[str]] = {start: None}
    g_score: Dict[str, float] = {start: 0.0}

    while open_heap:
        _, current = heappop(open_heap)
        if current == goal:
            path: List[str] = []
            while current is not None:
                path.append(current)
                current = came[current]
            path.reverse()
            return path
        for nb, w in adj.get(current, []):
            tentative = g_score[current] + w
            if tentative < g_score.get(nb, float("inf")):
                came[nb] = current
                g_score[nb] = tentative
                f = tentative + h(nb)
                heappush(open_heap, (f, nb))
    return None


def astar_forbidden(
    adj: Dict[str, List[Tuple[str, float]]],
    nodes_xy: Dict[str, Vec2],
    start: str,
    goal: str,
    forbidden: Set[str],
) -> Optional[List[str]]:
    """목표 노드는 금지 집합에 있어도 허용. 중간 통과만 차단."""
    if start == goal:
        return [start]
    if start not in adj or goal not in adj:
        return None

    def h(nid: str) -> float:
        return _euclid(nodes_xy[nid], nodes_xy[goal])

    open_heap: List[Tuple[float, str]] = []
    heappush(open_heap, (h(start), start))
    came: Dict[str, Optional[str]] = {start: None}
    g_score: Dict[str, float] = {start: 0.0}

    while open_heap:
        _, current = heappop(open_heap)
        if current == goal:
            path: List[str] = []
            while current is not None:
                path.append(current)
                current = came[current]
            path.reverse()
            return path
        for nb, w in adj.get(current, []):
            if nb in forbidden and nb != goal:
                continue
            tentative = g_score[current] + w
            if tentative < g_score.get(nb, float("inf")):
                came[nb] = current
                g_score[nb] = tentative
                f = tentative + h(nb)
                heappush(open_heap, (f, nb))
    return None


@dataclass
class WaveMapConfig:
    raw: Dict[str, Any]
    nodes_by_id: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    adj: Dict[str, List[Tuple[str, float]]] = field(default_factory=dict)
    label_to_node_id: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WaveMapConfig:
        nodes = {n["id"]: dict(n) for n in data.get("nodes") or [] if n.get("id")}
        for n in nodes.values():
            n["x"] = float(n.get("x", 0))
            n["y"] = float(n.get("y", 0))
            n["label"] = str(n.get("label") or "")
            n["role"] = str(n.get("role") or "waypoint")
        edges = data.get("edges") or []
        adj = _build_adjacency(nodes, edges)
        lbl: Dict[str, str] = {}
        for nid, n in nodes.items():
            lab = n.get("label") or ""
            if lab and lab not in lbl:
                lbl[lab] = nid
        return cls(raw=data, nodes_by_id=nodes, adj=adj, label_to_node_id=lbl)

    def stations_xy(self) -> Dict[str, Vec2]:
        out: Dict[str, Vec2] = {}
        for n in self.nodes_by_id.values():
            lab = n.get("label") or ""
            if lab:
                out[lab] = (float(n["x"]), float(n["y"]))
        return out

    def charging_labels(self) -> Set[str]:
        s: Set[str] = set()
        for n in self.nodes_by_id.values():
            if n.get("role") == "charger":
                lab = n.get("label") or ""
                if lab:
                    s.add(lab)
        return s

    def node_xy(self, nid: str) -> Optional[Vec2]:
        n = self.nodes_by_id.get(nid)
        if not n:
            return None
        return float(n["x"]), float(n["y"])

    def nearest_node_id(self, x: float, y: float) -> Optional[str]:
        best: Optional[str] = None
        best_d = float("inf")
        for nid, n in self.nodes_by_id.items():
            d = _euclid((x, y), (float(n["x"]), float(n["y"])))
            if d < best_d:
                best_d = d
                best = nid
        return best

    def path_for_labels(self, x0: float, y0: float, dest_label: str) -> Optional[List[Vec2]]:
        goal_id = self.label_to_node_id.get(dest_label)
        if not goal_id:
            return None
        start_id = self.nearest_node_id(x0, y0)
        if not start_id:
            return None
        nodes_xy = {nid: (float(n["x"]), float(n["y"])) for nid, n in self.nodes_by_id.items()}
        route = astar(self.adj, nodes_xy, start_id, goal_id)
        if not route:
            return None
        return [nodes_xy[i] for i in route]

    def path_for_labels_avoid_nodes(
        self,
        x0: float,
        y0: float,
        dest_label: str,
        avoid_node_ids: Set[str],
    ) -> Optional[List[Vec2]]:
        """정체 노드 회피 우회 경로 (없으면 None)."""
        goal_id = self.label_to_node_id.get(dest_label)
        if not goal_id:
            return None
        start_id = self.nearest_node_id(x0, y0)
        if not start_id:
            return None
        forbid = set(avoid_node_ids)
        forbid.discard(start_id)
        forbid.discard(goal_id)
        nodes_xy = {nid: (float(n["x"]), float(n["y"])) for nid, n in self.nodes_by_id.items()}
        route = astar_forbidden(self.adj, nodes_xy, start_id, goal_id, forbid)
        if not route:
            return None
        return [nodes_xy[i] for i in route]

    def graph_distance_m(self, x0: float, y0: float, dest_label: str) -> Optional[float]:
        pts = self.path_for_labels(x0, y0, dest_label)
        if not pts or len(pts) < 1:
            return None
        total = 0.0
        cx, cy = x0, y0
        for px, py in pts:
            total += _euclid((cx, cy), (px, py))
            cx, cy = px, py
        return total

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "version": self.raw.get("version", 1),
            "name": self.raw.get("name", "wave_map"),
            "imageDataUrl": self.raw.get("imageDataUrl") or "",
            "imageWidth": int(self.raw.get("imageWidth") or 0),
            "imageHeight": int(self.raw.get("imageHeight") or 0),
            "nodes": list(self.nodes_by_id.values()),
            "edges": list(self.raw.get("edges") or []),
        }


_config: Optional[WaveMapConfig] = None
_map_file_path: Optional[str] = None
_map_file_mtime: float = 0.0


def get_config() -> WaveMapConfig:
    """현재 맵 설정 반환. map.json 파일이 외부에서 변경된 경우 자동으로 리로드."""
    global _config, _map_file_path, _map_file_mtime
    if _map_file_path:
        try:
            mtime = os.path.getmtime(_map_file_path)
            if mtime != _map_file_mtime:
                _map_file_mtime = mtime
                with open(_map_file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _config = WaveMapConfig.from_dict(data)
                return _config
        except (OSError, json.JSONDecodeError):
            pass
    if _config is None:
        _config = WaveMapConfig.from_dict(_default_map_dict())
    return _config


def set_config(cfg: WaveMapConfig) -> None:
    global _config
    _config = cfg


def load_map_file(path: str) -> WaveMapConfig:
    global _map_file_path, _map_file_mtime
    _map_file_path = path
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = WaveMapConfig.from_dict(data)
        _map_file_mtime = os.path.getmtime(path)
    else:
        cfg = WaveMapConfig.from_dict(_default_map_dict())
        _map_file_mtime = 0.0
    set_config(cfg)
    return cfg


def save_map_file(path: str, data: Dict[str, Any]) -> WaveMapConfig:
    global _map_file_path, _map_file_mtime
    cfg = WaveMapConfig.from_dict(data)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _map_file_path = path
    _map_file_mtime = os.path.getmtime(path)
    set_config(cfg)
    return cfg
