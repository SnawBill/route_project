"""
Построитель маршрутов во Владивостоке для QGIS.

Пользователь кликает по карте дважды (точка A, затем точка B) — на карте появляется
маршрут по дорогам, а во всплывающей подсказке (Map Tip) — километраж и время.

Два режима работы (один и тот же интерфейс):
  backend="native"  — расчёт средствами QGIS: алгоритм Processing
                      «Кратчайший путь (из точки в точку)» по слою дорог roads.geojson;
  backend="service" — расчёт во внешнем Python-сервисе (app.py), QGIS обращается к нему по HTTP.

Запуск: Python-консоль QGIS (Ctrl+Alt+P) → значок «Показать редактор» → открыть этот файл → ▶
или в консоли:
    exec(open(r"C:\\путь\\qgis_route_tool.py", encoding="utf-8").read())
    start("service")                                  # через Python-сервис
    start("native", r"C:\\путь\\data\\roads.geojson")   # встроенными средствами QGIS

Управление: левый клик — поставить точку, кнопка на панели или правый клик — сбросить.
"""
import json
import math
import urllib.error
import urllib.parse
import urllib.request

import processing
from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
    QgsGeometry, QgsLineSymbol, QgsPointXY, QgsProcessingAlgRunnerTask,
    QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRasterLayer,
    QgsRectangle, QgsSimpleLineSymbolLayer, QgsTask, QgsVectorLayer,
)
from qgis.gui import QgsMapToolEmitPoint, QgsVertexMarker
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QStyle
from qgis.utils import iface

try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:
    from qgis.PyQt.QtGui import QAction

SERVICE_URL = "http://127.0.0.1:8000/route"
ROUTE_LAYER_NAME = "Маршрут"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
UTM52N = QgsCoordinateReferenceSystem("EPSG:32652")
WEB_MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")
AVG_CITY_SPEED_KMH = 25.0
START_DELAY_MIN = 1.0

MAP_TIP_HTML = """
<div style="font-family:'Segoe UI',Arial,sans-serif; min-width:230px; border:1px solid #90caf9;">
  <div style="background:#1565c0; color:#ffffff; padding:7px 12px; font-size:14px;">
    <b>&#128663; Маршрут по дорогам</b>
  </div>
  <div style="padding:10px 12px; background:#ffffff;">
    <div style="font-size:26px; color:#0d47a1;"><b>[% format_number("distance_km", 1) %] км</b></div>
    <div style="font-size:13px; color:#37474f; margin-top:2px;">
      &#9201; &asymp; [% "duration_min" %] мин в пути
    </div>
    <hr style="border:0; border-top:1px solid #e0e0e0; margin:8px 0;">
    <div style="font-size:11px; color:#78909c;">
      <span style="color:#2e7d32;"><b>A</b></span> &nbsp;[% "from_pt" %]<br>
      <span style="color:#c62828;"><b>B</b></span> &nbsp;[% "to_pt" %]<br>
      <i>расчёт: [% "engine" %]</i>
    </div>
  </div>
</div>
"""


class RouteError(Exception):
    pass


def estimate_duration_min(distance_km):
    return max(1, math.ceil(distance_km / AVG_CITY_SPEED_KMH * 60 + START_DELAY_MIN))


def ensure_basemap():
    """Подложка OpenStreetMap (XYZ-тайлы), если её ещё нет в проекте."""
    project = QgsProject.instance()
    if project.mapLayersByName("OpenStreetMap"):
        return
    uri = "type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=19&zmin=0"
    layer = QgsRasterLayer(uri, "OpenStreetMap", "wms")
    if layer.isValid():
        project.addMapLayer(layer, False)
        project.layerTreeRoot().insertLayer(-1, layer)
    else:
        iface.messageBar().pushWarning("Маршрут", "Не удалось загрузить подложку OpenStreetMap")


def zoom_to_vladivostok():
    canvas = iface.mapCanvas()
    QgsProject.instance().setCrs(WEB_MERCATOR)
    tr = QgsCoordinateTransform(WGS84, WEB_MERCATOR, QgsProject.instance())
    canvas.setExtent(tr.transformBoundingBox(QgsRectangle(131.80, 43.03, 132.05, 43.23)))
    canvas.refresh()


def load_roads(path):
    """Слой дорог для режима native: подгружаем GeoJSON и переводим в метры (UTM 52N),
    иначе стоимость пути посчиталась бы в градусах."""
    src = QgsVectorLayer(path, "Дороги (OSM)", "ogr")
    if not src.isValid():
        raise RouteError(f"Не удалось открыть слой дорог: {path}")
    if not QgsProject.instance().mapLayersByName("Дороги (OSM)"):
        symbol = QgsLineSymbol.createSimple({"line_color": "#b0bec5", "line_width": "0.2"})
        src.renderer().setSymbol(symbol)
        QgsProject.instance().addMapLayer(src)
    res = processing.run("native:reprojectlayer",
                         {"INPUT": src, "TARGET_CRS": UTM52N, "OUTPUT": "TEMPORARY_OUTPUT"})
    return res["OUTPUT"]


def get_route_layer():
    """Слой с результатом (создаётся один раз): линия + атрибуты + стиль + всплывающая подсказка."""
    project = QgsProject.instance()
    existing = project.mapLayersByName(ROUTE_LAYER_NAME)
    if existing:
        return existing[0]

    uri = ("LineString?crs=EPSG:4326&field=distance_km:double&field=duration_min:integer"
           "&field=from_pt:string&field=to_pt:string&field=engine:string")
    layer = QgsVectorLayer(uri, ROUTE_LAYER_NAME, "memory")

    symbol = QgsLineSymbol.createSimple({"line_color": "#0d47a1", "line_width": "2.6",
                                         "capstyle": "round", "joinstyle": "round"})
    inner = QgsSimpleLineSymbolLayer(QColor("#42a5f5"), 1.5)
    inner.setPenCapStyle(Qt.RoundCap)
    inner.setPenJoinStyle(Qt.RoundJoin)
    symbol.appendSymbolLayer(inner)
    layer.renderer().setSymbol(symbol)

    layer.setMapTipTemplate(MAP_TIP_HTML)
    project.addMapLayer(layer)
    return layer


def enable_map_tips(layer):
    """Включает «Вид → Показывать всплывающие подсказки» и делает слой активным
    (подсказки QGIS показывает только для активного слоя)."""
    iface.setActiveLayer(layer)
    action = iface.mainWindow().findChild(QAction, "mActionMapTips")
    if action is not None and not action.isChecked():
        action.trigger()


def route_via_service(a, b):
    query = urllib.parse.urlencode({"from_lon": a.x(), "from_lat": a.y(),
                                    "to_lon": b.x(), "to_lat": b.y()})
    try:
        with urllib.request.urlopen(f"{SERVICE_URL}?{query}", timeout=60) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc).get("error", str(exc))
        except Exception:
            message = str(exc)
        raise RouteError(message)
    except urllib.error.URLError:
        raise RouteError(f"Сервис маршрутов недоступен ({SERVICE_URL}). Запущен ли uv run app.py?")
    props = data["properties"]
    coords = [(x, y) for x, y in data["geometry"]["coordinates"]]
    return coords, props["distance_km"], props["duration_min"]


def qgis_route_parameters(a, b, roads_utm):
    to_utm = QgsCoordinateTransform(WGS84, UTM52N, QgsProject.instance())
    pa, pb = to_utm.transform(a), to_utm.transform(b)
    return {
        "INPUT": roads_utm,
        "STRATEGY": 0,
        "DIRECTION_FIELD": "dir",
        "VALUE_FORWARD": "forward",
        "VALUE_BACKWARD": "",
        "VALUE_BOTH": "both",
        "DEFAULT_DIRECTION": 2,
        "TOLERANCE": 0,
        "START_POINT": f"{pa.x()},{pa.y()} [EPSG:32652]",
        "END_POINT": f"{pb.x()},{pb.y()} [EPSG:32652]",
        "OUTPUT": "TEMPORARY_OUTPUT",
    }


class RouteTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, backend="service", roads_utm=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.backend = backend
        self.roads_utm = roads_utm
        self.points = []
        self.markers = []
        self.busy = False
        self.task = None
        self.context = None

    def canvasReleaseEvent(self, event):
        if self.busy:
            iface.messageBar().pushInfo("Маршрут", "Расчёт уже выполняется…")
            return
        if event.button() == Qt.RightButton:
            self.reset()
            return
        if len(self.points) == 2:
            self.reset()
        map_pt = self.toMapCoordinates(event.pos())
        to_wgs = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), WGS84,
                                        QgsProject.instance())
        self.points.append(to_wgs.transform(map_pt))
        self._add_marker(map_pt, "#2e7d32" if len(self.points) == 1 else "#c62828")

        if len(self.points) == 1:
            iface.messageBar().pushInfo("Маршрут", "Точка A выбрана. Кликните точку B.")
        else:
            self.build_route()

    def deactivate(self):
        super().deactivate()

    def _add_marker(self, map_pt, color):
        m = QgsVertexMarker(self.canvas)
        m.setCenter(map_pt)
        m.setColor(QColor(color))
        m.setFillColor(QColor(color))
        m.setIconType(QgsVertexMarker.ICON_CIRCLE)
        m.setIconSize(14)
        m.setPenWidth(3)
        self.markers.append(m)

    def reset(self):
        for m in self.markers:
            self.canvas.scene().removeItem(m)
        self.markers.clear()
        self.points.clear()
        layer = QgsProject.instance().mapLayersByName(ROUTE_LAYER_NAME)
        if layer:
            layer[0].dataProvider().truncate()
            layer[0].triggerRepaint()

    def build_route(self):
        a, b = self.points
        self.busy = True
        iface.messageBar().pushInfo("Маршрут", "Рассчитываю маршрут…")
        if self.backend == "native":
            algorithm = QgsApplication.processingRegistry().algorithmById(
                "native:shortestpathpointtopoint")
            self.context = QgsProcessingContext()
            self.context.setProject(QgsProject.instance())
            feedback = QgsProcessingFeedback()
            self.task = QgsProcessingAlgRunnerTask(
                algorithm, qgis_route_parameters(a, b, self.roads_utm), self.context, feedback)
            self.task.executed.connect(self._native_finished)
        else:
            self.task = QgsTask.fromFunction(
                "Расчёт маршрута", lambda task: route_via_service(a, b),
                on_finished=lambda exception, result=None: self._service_finished(
                    exception, result, a, b))
        QgsApplication.taskManager().addTask(self.task)

    def _service_finished(self, exception, result, a, b):
        if exception:
            self._failed(exception)
            return
        self._show_route(result, a, b, "Python-сервис")

    def _native_finished(self, successful, results):
        if not successful:
            self._failed(RouteError("Не удалось построить маршрут средствами QGIS"))
            return
        out = self.context.getMapLayer(results["OUTPUT"])
        features = list(out.getFeatures()) if out else []
        if not features:
            self._failed(RouteError("Проезд между этими точками не найден"))
            return
        geom = QgsGeometry(features[0].geometry())
        km = round(geom.length() / 1000.0, 2)
        geom.transform(QgsCoordinateTransform(UTM52N, WGS84, QgsProject.instance()))
        coords = [(p.x(), p.y()) for p in (geom.asMultiPolyline()[0] if geom.isMultipart()
                                            else geom.asPolyline())]
        minutes = estimate_duration_min(km)
        self._show_route((coords, km, minutes), *self.points, "QGIS (Processing)")

    def _failed(self, exception):
        self.busy = False
        self.task = None
        self.context = None
        iface.messageBar().pushWarning("Маршрут", str(exception))
        self.reset()

    def _show_route(self, result, a, b, engine):
        coords, km, minutes = result
        self.busy = False
        self.task = None
        self.context = None

        layer = get_route_layer()
        layer.dataProvider().truncate()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in coords]))
        feat.setAttributes([km, minutes, f"{a.y():.5f}, {a.x():.5f}", f"{b.y():.5f}, {b.x():.5f}", engine])
        layer.dataProvider().addFeatures([feat])
        layer.updateExtents()
        layer.triggerRepaint()
        enable_map_tips(layer)

        to_canvas = QgsCoordinateTransform(WGS84, self.canvas.mapSettings().destinationCrs(),
                                           QgsProject.instance())
        extent = to_canvas.transformBoundingBox(layer.extent())
        extent.scale(1.4)
        self.canvas.setExtent(extent)
        self.canvas.refresh()

        iface.messageBar().pushSuccess(
            "Маршрут построен", f"{km:.1f} км, ≈ {minutes} мин. Наведите курсор на линию маршрута.")


_tool = None
_clear_action = None


def clear_route(checked=False):
    if _tool is None:
        return
    if _tool.busy:
        iface.messageBar().pushInfo("Маршрут", "Дождитесь окончания расчёта.")
        return
    _tool.reset()
    iface.messageBar().pushSuccess("Маршрут", "Маршрут очищен.")


def install_clear_action():
    action = iface.mainWindow().findChild(QAction, "routeProjectClearAction")
    if action is None:
        icon = iface.mainWindow().style().standardIcon(QStyle.SP_DialogResetButton)
        action = QAction(icon, "Очистить маршрут", iface.mainWindow())
        action.setObjectName("routeProjectClearAction")
        action.setToolTip("Очистить маршрут и выбранные точки")
        iface.addToolBarIcon(action)
    else:
        try:
            action.triggered.disconnect()
        except TypeError:
            pass
    action.triggered.connect(clear_route)
    return action


def start(backend="service", roads_geojson=None):
    """backend: 'service' или 'native'. Для 'native' укажите путь к roads.geojson."""
    global _tool, _clear_action
    if backend not in ("service", "native"):
        raise ValueError("backend должен быть 'service' или 'native'")
    roads_utm = None
    if backend == "native":
        if not roads_geojson:
            raise ValueError("Для backend='native' укажите путь к roads.geojson")
        roads_utm = load_roads(roads_geojson)

    ensure_basemap()
    zoom_to_vladivostok()
    canvas = iface.mapCanvas()
    _tool = RouteTool(canvas, backend, roads_utm)
    canvas.setMapTool(_tool)
    _clear_action = install_clear_action()
    iface.messageBar().pushInfo(
        "Маршрут", "Кликните точку A и точку B на карте Владивостока (правый клик — сброс).")
    return _tool
