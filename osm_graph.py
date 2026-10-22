"""
Загрузка дорожной сети из OpenStreetMap (Overpass API) и сборка графа для маршрутизации.

Из одних и тех же данных строятся два артефакта:
  * graph.pkl      — ориентированный граф networkx для Python-сервиса (app.py);
  * roads.geojson  — линии дорог для QGIS (вариант «средствами QGIS»).

Зависимости: networkx (+ стандартная библиотека).
"""
from __future__ import annotations

import json
import math
import pickle
import re
import urllib.parse
import urllib.request

import networkx as nx

# Владивосток вместе с островом Русский: (юг, запад, север, восток)
VLADIVOSTOK_BBOX = (42.90, 131.70, 43.35, 132.15)

OVERPASS_URLS = (
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)

# Типы дорог, по которым ездят автомобили, и скорости по умолчанию (км/ч).
DEFAULT_SPEED_KMH = {
    "motorway": 90, "motorway_link": 60,
    "trunk": 70, "trunk_link": 50,
    "primary": 60, "primary_link": 40,
    "secondary": 50, "secondary_link": 40,
    "tertiary": 40, "tertiary_link": 30,
    "unclassified": 40,
    "residential": 30,
    "living_street": 15,
    "service": 20,
}
MAX_SPEED_KMH = max(DEFAULT_SPEED_KMH.values())

_EARTH_R = 6_371_008.8


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Расстояние между двумя точками на сфере, метры."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(a))


def overpass_query(bbox=VLADIVOSTOK_BBOX) -> str:
    s, w, n, e = bbox
    types = "|".join(DEFAULT_SPEED_KMH)
    return (
        "[out:json][timeout:300];"
        f'way["highway"~"^({types})$"]({s},{w},{n},{e});'
        "(._;>;);out body;"
    )


def fetch_overpass(bbox=VLADIVOSTOK_BBOX, timeout: int = 400) -> dict:
    data = urllib.parse.urlencode({"data": overpass_query(bbox)}).encode()
    last_error = None
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(
                url, data=data, headers={"User-Agent": "vladivostok-route-service/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as exc:  # пробуем следующее зеркало
            last_error = exc
            print(f"[!] {url}: {exc}")
    raise RuntimeError(f"Не удалось скачать данные Overpass: {last_error}")


def parse_maxspeed(value) -> float | None:
    """'50' -> 50, '30 mph' -> 48.3, 'RU:urban' -> None."""
    if not value:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(mph)?", str(value))
    if not m:
        return None
    speed = float(m.group(1))
    return speed * 1.609344 if m.group(2) else speed


def oneway_direction(tags: dict) -> int:
    """1 — только по направлению линии, -1 — против, 0 — в обе стороны."""
    ow = tags.get("oneway")
    if ow in ("yes", "true", "1"):
        return 1
    if ow == "-1":
        return -1
    if ow in ("no", "false", "0"):
        return 0
    if tags.get("junction") in ("roundabout", "circular"):
        return 1
    if tags.get("highway") in ("motorway", "motorway_link"):
        return 1
    return 0


def is_drivable(tags: dict) -> bool:
    if tags.get("highway") not in DEFAULT_SPEED_KMH:
        return False
    if tags.get("access") in ("no", "private") or tags.get("motor_vehicle") in ("no", "private"):
        return False
    if tags.get("highway") == "service" and tags.get("service") in ("parking_aisle", "driveway"):
        return False
    return True


def way_speed_kmh(tags: dict) -> float:
    speed = parse_maxspeed(tags.get("maxspeed"))
    if speed is None or speed <= 0:
        speed = DEFAULT_SPEED_KMH[tags["highway"]]
    return speed


def _split(overpass_json: dict):
    nodes, ways = {}, []
    for el in overpass_json.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el["type"] == "way" and is_drivable(el.get("tags", {})):
            ways.append(el)
    return nodes, ways


def build_graph(overpass_json: dict) -> nx.DiGraph:
    """Ориентированный граф: узел = перекрёсток/точка OSM, ребро = отрезок дороги.

    Атрибуты узла: x (долгота), y (широта).
    Атрибуты ребра: length (м), time (с), highway, name.
    """
    nodes, ways = _split(overpass_json)
    G = nx.DiGraph()

    for way in ways:
        tags = way["tags"]
        refs = [r for r in way["nodes"] if r in nodes]
        if len(refs) < 2:
            continue
        direction = oneway_direction(tags)
        speed_ms = way_speed_kmh(tags) / 3.6
        for u, v in zip(refs, refs[1:]):
            if u == v:
                continue
            (x1, y1), (x2, y2) = nodes[u], nodes[v]
            length = haversine_m(x1, y1, x2, y2)
            attrs = dict(length=length, time=length / speed_ms,
                         highway=tags["highway"], name=tags.get("name", ""))
            for n, (x, y) in ((u, (x1, y1)), (v, (x2, y2))):
                if n not in G:
                    G.add_node(n, x=x, y=y)
            pairs = []
            if direction >= 0:
                pairs.append((u, v))
            if direction <= 0:
                pairs.append((v, u))
            for a, b in pairs:
                # если между узлами уже есть ребро (две дороги) — берём более короткое
                if not G.has_edge(a, b) or G[a][b]["length"] > length:
                    G.add_edge(a, b, **attrs)

    if G.number_of_nodes() == 0:
        raise ValueError("В данных нет подходящих дорог")

    # Оставляем самую большую сильно связную часть: из неё всегда можно проехать в любую точку.
    biggest = max(nx.strongly_connected_components(G), key=len)
    return G.subgraph(biggest).copy()


def save_graph(G: nx.DiGraph, path: str) -> None:
    with open(path, "wb") as fh:
        pickle.dump(G, fh, protocol=pickle.HIGHEST_PROTOCOL)


def load_graph(path: str) -> nx.DiGraph:
    with open(path, "rb") as fh:
        return pickle.load(fh)


def roads_geojson(overpass_json: dict) -> dict:
    """Одна линия на дорогу. Поле dir: 'forward' — односторонняя (по направлению линии),
    'both' — двустороннее движение. Нужно алгоритму QGIS «Кратчайший путь»."""
    nodes, ways = _split(overpass_json)
    features = []
    for way in ways:
        tags = way["tags"]
        coords = [list(nodes[r]) for r in way["nodes"] if r in nodes]
        if len(coords) < 2:
            continue
        direction = oneway_direction(tags)
        if direction == -1:
            coords.reverse()
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "highway": tags["highway"],
                "name": tags.get("name", ""),
                "dir": "forward" if direction != 0 else "both",
                "speed_kmh": round(way_speed_kmh(tags), 1),
            },
        })
    return {"type": "FeatureCollection", "features": features}
