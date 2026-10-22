"""Ядро маршрутизации: привязка точки к дороге и поиск пути по графу (A*)."""
from __future__ import annotations

import math
from dataclasses import dataclass

import networkx as nx

from osm_graph import MAX_SPEED_KMH, haversine_m, load_graph

CITY_AVG_SPEED_KMH = 25.0
START_DELAY_MIN = 1.0


def estimate_duration_min(length_m: float, free_flow_s: float = 0.0) -> int:
    """Городская оценка времени: свободный поток, но не быстрее 25 км/ч, плюс старт."""
    city_minutes = length_m / 1000.0 / CITY_AVG_SPEED_KMH * 60.0
    return max(1, math.ceil(max(free_flow_s / 60.0, city_minutes) + START_DELAY_MIN))


class RoutingError(Exception):
    """Ошибка, понятная пользователю. status — HTTP-код для API."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class Route:
    coords: list
    length_m: float
    time_s: float
    start_node: int
    end_node: int
    start_snap_m: float
    end_snap_m: float


class RoadNetwork:
    def __init__(self, G: nx.DiGraph, max_snap_m: float = 500.0):
        if G.number_of_nodes() == 0:
            raise ValueError("Пустой граф")
        self.G = G
        self.max_snap_m = max_snap_m
        self.ids = list(G.nodes)
        lon = [G.nodes[n]["x"] for n in self.ids]
        lat = [G.nodes[n]["y"] for n in self.ids]
        # Локальная равнопромежуточная проекция: в пределах города ошибка пренебрежимо мала.
        self._kx = 111_320.0 * math.cos(math.radians(sum(lat) / len(lat)))
        self._ky = 110_574.0
        self._xy = [(x * self._kx, y * self._ky) for x, y in zip(lon, lat)]
        self._cell_size = max_snap_m
        self._grid = {}
        for i, (x, y) in enumerate(self._xy):
            cell = (math.floor(x / self._cell_size), math.floor(y / self._cell_size))
            self._grid.setdefault(cell, []).append(i)
        self.bbox = (min(lon), min(lat), max(lon), max(lat))

    @classmethod
    def from_file(cls, path: str, **kwargs) -> "RoadNetwork":
        return cls(load_graph(path), **kwargs)

    def snap(self, lon: float, lat: float):
        """Ближайший узел дорожной сети и расстояние до него (м)."""
        w, s, e, n = self.bbox
        margin = 0.02  # ~2 км за границей данных
        if not (w - margin <= lon <= e + margin and s - margin <= lat <= n + margin):
            raise RoutingError("Точка вне зоны покрытия дорожной сети (Владивосток)")
        x, y = lon * self._kx, lat * self._ky
        cx, cy = math.floor(x / self._cell_size), math.floor(y / self._cell_size)
        candidates = [
            i
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for i in self._grid.get((cx + dx, cy + dy), ())
        ]
        if not candidates:
            raise RoutingError("Рядом с точкой нет дорог. Выберите точку ближе к дороге.")
        dist2, idx = min(
            (((self._xy[i][0] - x) ** 2 + (self._xy[i][1] - y) ** 2, i)
             for i in candidates),
            key=lambda item: item[0],
        )
        dist = math.sqrt(dist2)
        if dist > self.max_snap_m:
            raise RoutingError(
                f"Рядом с точкой нет дорог (ближайшая в {dist:.0f} м). Выберите точку ближе к дороге."
            )
        return self.ids[idx], float(dist)

    def route(self, a: tuple, b: tuple, mode: str = "shortest") -> Route:
        """a, b — (lon, lat). mode: 'shortest' (по длине) или 'fastest' (по времени)."""
        if mode not in ("shortest", "fastest"):
            raise RoutingError("mode должен быть 'shortest' или 'fastest'", 400)
        s, s_m = self.snap(*a)
        t, t_m = self.snap(*b)
        if s == t:
            raise RoutingError("Начальная и конечная точки привязались к одному перекрёстку")

        G = self.G
        tx, ty = G.nodes[t]["x"], G.nodes[t]["y"]
        if mode == "shortest":
            weight = "length"
            heuristic = lambda u, v: 0.99 * haversine_m(G.nodes[u]["x"], G.nodes[u]["y"], tx, ty)
        else:
            weight = "time"
            vmax = MAX_SPEED_KMH / 3.6
            heuristic = lambda u, v: 0.99 * haversine_m(G.nodes[u]["x"], G.nodes[u]["y"], tx, ty) / vmax

        try:
            path = nx.astar_path(G, s, t, heuristic=heuristic, weight=weight)
        except nx.NetworkXNoPath:
            raise RoutingError("Проезд между этими точками не найден", 404)

        edges = list(zip(path, path[1:]))
        return Route(
            coords=[(G.nodes[n]["x"], G.nodes[n]["y"]) for n in path],
            length_m=sum(G[u][v]["length"] for u, v in edges),
            time_s=sum(G[u][v]["time"] for u, v in edges),
            start_node=s, end_node=t, start_snap_m=s_m, end_snap_m=t_m,
        )
