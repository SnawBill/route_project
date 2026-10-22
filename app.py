"""
HTTP-сервис построения маршрутов по дорогам Владивостока.

Запуск:
    uv run app.py                      # http://127.0.0.1:8000
    GRAPH_PATH=data/graph.pkl PORT=8000 uv run app.py

API:
    GET /route?from_lon=131.89&from_lat=43.11&to_lon=131.93&to_lat=43.14[&mode=shortest|fastest]
        -> GeoJSON Feature (LineString) со свойствами distance_km, duration_min, ...
    GET /health
"""
import os
import threading

from flask import Flask, jsonify, request

from routing import RoadNetwork, RoutingError, estimate_duration_min

app = Flask(__name__)
app.json.ensure_ascii = False
app.json.sort_keys = False

GRAPH_PATH = os.environ.get("GRAPH_PATH", os.path.join(os.path.dirname(__file__), "data", "graph.pkl"))

_network = None
_lock = threading.Lock()


def get_network() -> RoadNetwork:
    """Граф загружается один раз."""
    global _network
    if _network is None:
        with _lock:
            if _network is None:
                if not os.path.exists(GRAPH_PATH):
                    raise RoutingError(
                        f"Нет файла графа {GRAPH_PATH}. Сначала выполните: uv run build_graph.py", 503
                    )
                _network = RoadNetwork.from_file(GRAPH_PATH)
    return _network


def _float_arg(name: str) -> float:
    raw = request.args.get(name)
    if raw is None:
        raise RoutingError(f"Не указан параметр {name}", 400)
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        raise RoutingError(f"Параметр {name} должен быть числом", 400)
    return value


@app.errorhandler(RoutingError)
def handle_routing_error(err: RoutingError):
    return jsonify(error=str(err)), err.status


@app.get("/health")
def health():
    net = get_network()
    return jsonify(status="ok", nodes=net.G.number_of_nodes(), edges=net.G.number_of_edges(),
                   bbox=net.bbox)


@app.get("/route")
def route():
    a = (_float_arg("from_lon"), _float_arg("from_lat"))
    b = (_float_arg("to_lon"), _float_arg("to_lat"))
    for lon, lat in (a, b):
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise RoutingError("Координаты вне допустимого диапазона (lon -180..180, lat -90..90)", 400)
    mode = request.args.get("mode", "shortest")

    r = get_network().route(a, b, mode=mode)
    return jsonify({
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [list(c) for c in r.coords]},
        "properties": {
            "distance_km": round(r.length_m / 1000, 2),
            "duration_min": estimate_duration_min(r.length_m, r.time_s),
            "mode": mode,
            "from": {"lon": a[0], "lat": a[1], "snap_m": round(r.start_snap_m)},
            "to": {"lon": b[0], "lat": b[1], "snap_m": round(r.end_snap_m)},
        },
    })


if __name__ == "__main__":
    get_network()
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")),
            threaded=True)
