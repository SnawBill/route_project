# Маршруты Владивостока в QGIS

Инструмент строит автомобильный маршрут между двумя точками, считает расстояние и показывает информацию во всплывающей карточке.

Доступны два режима:

- `native` — расчёт алгоритмом QGIS Processing;
- `service` — расчёт Python-сервисом на Flask и NetworkX.

## Подготовка

Нужны QGIS 3.x, Python 3.10+ и [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run build_graph.py
```

Команда создаст `data/graph.pkl` для Python-сервиса и `data/roads.geojson` для QGIS.

## Python-сервис

```bash
uv run app.py
```

Проверка: <http://127.0.0.1:8000/health>

```text
GET /route?from_lon=131.89&from_lat=43.11&to_lon=131.93&to_lat=43.14
```

## QGIS

Откройте **Плагины → Консоль Python** и загрузите инструмент:

```python
exec(open("/путь/к/route_project/qgis_route_tool.py", encoding="utf-8").read())
```

Через Python-сервис:

```python
start("service")
```

Средствами QGIS:

```python
start("native", "/путь/к/route_project/data/roads.geojson")
```

Левый клик выбирает точки A и B, правый очищает маршрут. При наведении на линию появляется карточка с расстоянием и временем.

## Проверка

```bash
uv run python -m unittest discover -s tests -v
```
