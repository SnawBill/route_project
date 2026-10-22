"""Проверка сервиса на синтетической дорожной сети (без интернета).

Запуск:  uv run python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import osm_graph
from routing import RoadNetwork, RoutingError, estimate_duration_min

LON0, LAT0, STEP = 131.88, 43.10, 0.01   # сетка 6x6 узлов, шаг ~0.8 км по долготе, ~1.1 км по широте
N = 6


def nid(i, j):
    return 1000 + i * N + j


def synthetic_overpass():
    els = []
    for i in range(N):
        for j in range(N):
            els.append({"type": "node", "id": nid(i, j), "lon": LON0 + j * STEP, "lat": LAT0 + i * STEP})
    wid = 1
    # горизонтальные улицы (i = const); улица i=2 — односторонняя (на восток)
    for i in range(N):
        tags = {"highway": "residential", "name": f"Горизонтальная {i}"}
        if i == 2:
            tags["oneway"] = "yes"
        els.append({"type": "way", "id": wid, "nodes": [nid(i, j) for j in range(N)], "tags": tags})
        wid += 1
    # вертикальные улицы (j = const)
    for j in range(N):
        els.append({"type": "way", "id": wid, "nodes": [nid(i, j) for i in range(N)],
                    "tags": {"highway": "secondary", "name": f"Вертикальная {j}"}})
        wid += 1
    # пешеходная дорожка — по ней ехать нельзя; соединяет углы по диагонали
    els.append({"type": "way", "id": wid, "nodes": [nid(0, 0), nid(N - 1, N - 1)],
                "tags": {"highway": "footway"}})
    # закрытая дорога
    wid += 1
    els.append({"type": "way", "id": wid, "nodes": [nid(0, 0), nid(0, N - 1)],
                "tags": {"highway": "service", "access": "private"}})
    return {"elements": els}


def lonlat(i, j):
    return LON0 + j * STEP, LAT0 + i * STEP


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = synthetic_overpass()
        cls.G = osm_graph.build_graph(cls.raw)
        cls.net = RoadNetwork(cls.G)

    def test_graph_excludes_non_drivable(self):
        self.assertEqual(self.G.number_of_nodes(), N * N)
        # по диагонали (пешеходная дорожка) напрямую не соединено
        self.assertFalse(self.G.has_edge(nid(0, 0), nid(N - 1, N - 1)))

    def test_distance_matches_manhattan(self):
        a, b = lonlat(0, 0), lonlat(0, N - 1)
        r = self.net.route(a, b)
        expected = sum(osm_graph.haversine_m(*lonlat(0, j), *lonlat(0, j + 1)) for j in range(N - 1))
        self.assertAlmostEqual(r.length_m, expected, delta=1.0)
        self.assertEqual(len(r.coords), N)

    def test_oneway_is_respected(self):
        # По улице i=2 можно ехать только на восток. Едем на запад: маршрут должен
        # обойти по соседним улицам и быть длиннее прямой.
        east = self.net.route(lonlat(2, 0), lonlat(2, N - 1))
        west = self.net.route(lonlat(2, N - 1), lonlat(2, 0))
        direct = sum(osm_graph.haversine_m(*lonlat(2, j), *lonlat(2, j + 1)) for j in range(N - 1))
        self.assertAlmostEqual(east.length_m, direct, delta=1.0)
        self.assertGreater(west.length_m, direct + 500)

    def test_astar_equals_dijkstra(self):
        import networkx as nx
        a, b = lonlat(0, 1), lonlat(5, 4)
        r = self.net.route(a, b)
        expected = nx.dijkstra_path_length(self.G, nid(0, 1), nid(5, 4), weight="length")
        self.assertAlmostEqual(r.length_m, expected, delta=0.01)

    def test_snapping_far_point(self):
        with self.assertRaises(RoutingError):
            self.net.route((LON0 + 0.0045, LAT0 + 0.0045), lonlat(0, 0))  # ~600 м от ближайшего узла
        with self.assertRaises(RoutingError):
            self.net.route((139.0, 35.0), lonlat(0, 0))                   # далеко за пределами

    def test_same_node(self):
        with self.assertRaises(RoutingError):
            self.net.route(lonlat(1, 1), (lonlat(1, 1)[0] + 0.0001, lonlat(1, 1)[1]))

    def test_fastest_mode(self):
        r = self.net.route(lonlat(0, 0), lonlat(5, 5), mode="fastest")
        self.assertGreater(r.time_s, 0)

    def test_city_duration_is_not_free_flow(self):
        self.assertEqual(estimate_duration_min(1_000, 60), 4)

    def test_roads_geojson(self):
        gj = osm_graph.roads_geojson(self.raw)
        props = [f["properties"] for f in gj["features"]]
        self.assertTrue(all(p["highway"] != "footway" for p in props))
        self.assertIn("forward", {p["dir"] for p in props})
        self.assertIn("both", {p["dir"] for p in props})


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "graph.pkl")
        osm_graph.save_graph(osm_graph.build_graph(synthetic_overpass()), path)
        os.environ["GRAPH_PATH"] = path
        import app as app_module
        app_module.GRAPH_PATH = path
        app_module._network = None
        cls.client = app_module.app.test_client()

    def q(self, **kw):
        return self.client.get("/route", query_string=kw)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["nodes"], N * N)

    def test_route_ok(self):
        (a_lon, a_lat), (b_lon, b_lat) = lonlat(0, 0), lonlat(0, N - 1)
        r = self.q(from_lon=a_lon, from_lat=a_lat, to_lon=b_lon, to_lat=b_lat)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["geometry"]["type"], "LineString")
        self.assertAlmostEqual(data["properties"]["distance_km"], 4.0, delta=0.2)
        self.assertGreaterEqual(data["properties"]["duration_min"], 1)

    def test_decimal_comma(self):
        r = self.q(**{"from_lon": "131,88", "from_lat": "43,10", "to_lon": "131,93", "to_lat": "43,10"})
        self.assertEqual(r.status_code, 200)

    def test_errors(self):
        self.assertEqual(self.q(from_lon=1).status_code, 400)
        self.assertEqual(self.q(from_lon="x", from_lat=1, to_lon=1, to_lat=1).status_code, 400)
        self.assertEqual(self.q(from_lon=999, from_lat=1, to_lon=1, to_lat=1).status_code, 400)
        r = self.q(from_lon=10, from_lat=10, to_lon=11, to_lat=11)
        self.assertEqual(r.status_code, 422)
        self.assertIn("error", r.get_json())


if __name__ == "__main__":
    unittest.main()
