"""Автоматический пользовательский сценарий для демонстрации в QGIS.

Запуск:
    qgis --nologo --code qgis_demo.py

Python-сервис ``uv run app.py`` должен быть запущен заранее.
"""
import os
import sys
import traceback

from qgis.core import QgsCoordinateTransform, QgsPointXY, QgsProject
from qgis.gui import QgsMapTip
from qgis.PyQt.QtCore import QPoint, QTimer
from qgis.utils import iface

project_dir = next(
    (os.path.dirname(os.path.abspath(arg)) for arg in sys.argv if arg.endswith("qgis_demo.py")),
    os.getcwd(),
)
sys.path.insert(0, project_dir)
import qgis_route_tool as routes


START = QgsPointXY(131.8855, 43.1155)
END = QgsPointXY(131.9365, 43.1280)

iface.mainWindow().showMaximized()
tool = routes.start("service")
canvas = iface.mapCanvas()
tip = QgsMapTip()


def _canvas_point(point):
    transform = QgsCoordinateTransform(routes.WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
    return transform.transform(point)


def choose_start():
    tool.points.append(START)
    tool._add_marker(_canvas_point(START), "#2e7d32")
    iface.messageBar().pushInfo("Маршрут", "Точка A выбрана. Кликните точку B.")


def choose_end():
    tool.points.append(END)
    tool._add_marker(_canvas_point(END), "#c62828")
    tool.build_route()


def show_route_tip():
    layer = QgsProject.instance().mapLayersByName(routes.ROUTE_LAYER_NAME)[0]
    feature = next(layer.getFeatures())
    midpoint = feature.geometry().interpolate(feature.geometry().length() / 2).asPoint()
    map_point = _canvas_point(QgsPointXY(midpoint))
    pixel_point = canvas.getCoordinateTransform().transform(map_point)
    tip.showMapTip(layer, map_point, QPoint(round(pixel_point.x()), round(pixel_point.y())), canvas)


def safely(callback):
    try:
        callback()
    except Exception:
        traceback.print_exc()


QTimer.singleShot(3000, routes.zoom_to_vladivostok)
QTimer.singleShot(9000, lambda: safely(choose_start))
QTimer.singleShot(12000, lambda: safely(choose_end))
QTimer.singleShot(15500, lambda: safely(show_route_tip))
