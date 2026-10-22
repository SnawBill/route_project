"""
Скачивает дороги Владивостока из OpenStreetMap и готовит данные:
    data/graph.pkl      — граф для Python-сервиса
    data/roads.geojson  — дороги для QGIS

Запуск (нужен интернет, 1–3 минуты):
    uv run build_graph.py
Или из ранее сохранённого ответа Overpass:
    uv run build_graph.py --from-json data/overpass.json
"""
import argparse
import json
import os

import osm_graph


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--from-json", help="готовый ответ Overpass (пропустить скачивание)")
    ap.add_argument("--save-raw", action="store_true", help="сохранить сырой ответ Overpass")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        print("Скачиваю дороги из OpenStreetMap (Overpass API)...")
        raw = osm_graph.fetch_overpass()
        if args.save_raw:
            with open(os.path.join(args.out_dir, "overpass.json"), "w", encoding="utf-8") as fh:
                json.dump(raw, fh, ensure_ascii=False)

    print("Строю граф...")
    G = osm_graph.build_graph(raw)
    graph_path = os.path.join(args.out_dir, "graph.pkl")
    osm_graph.save_graph(G, graph_path)
    print(f"  {graph_path}: {G.number_of_nodes()} узлов, {G.number_of_edges()} рёбер")

    roads = osm_graph.roads_geojson(raw)
    roads_path = os.path.join(args.out_dir, "roads.geojson")
    with open(roads_path, "w", encoding="utf-8") as fh:
        json.dump(roads, fh, ensure_ascii=False)
    print(f"  {roads_path}: {len(roads['features'])} дорог")
    print("Готово.")


if __name__ == "__main__":
    main()
