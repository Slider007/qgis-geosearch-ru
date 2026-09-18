"""Проверки плагина без интерфейса QGIS и без обращения к DaData. Запуск: tests/run_tests.sh

Ответы DaData подменяются поддельными, холст карты и окно плагина — настоящие.
Скрипт не трогает профиль QGIS: настройки пишутся во временный профиль tests/_profile.
"""

import json
import os
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from qgis.PyQt.QtCore import QEvent, QCoreApplication, QSettings  # noqa: E402

PROFILE = os.path.join(HERE, "_profile")
shutil.rmtree(PROFILE, ignore_errors=True)
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
QCoreApplication.setOrganizationName("geosearch-ru-tests")
QCoreApplication.setApplicationName("geosearch-ru-tests")

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsProject  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtNetwork import QNetworkReply  # noqa: E402
from qgis.PyQt.QtWidgets import QLabel, QMainWindow, QToolBar  # noqa: E402

APP = QgsApplication([], True)
APP.initQgis()

import GeoSearchRU  # noqa: E402

NO_ERROR = QNetworkReply.NetworkError.NoError

SUGGESTIONS = {"suggestions": [
    {"value": "г Тамбов, пр-кт Энергетиков, д 7",
     "unrestricted_value": "392000, Тамбовская обл, г Тамбов, пр-кт Энергетиков, д 7",
     "data": {"geo_lat": "52.72", "geo_lon": "41.45", "qc_geo": "0"}},
    {"value": "г Тамбов", "data": {"geo_lat": "52.72", "geo_lon": "41.45", "qc_geo": "4"}},
    {"value": "г Тамбов, ул Несуществующая", "data": {"geo_lat": None, "geo_lon": None, "qc_geo": "5"}},
]}


class Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)


class FakeReply:
    def __init__(self, body, error=NO_ERROR, status=200, error_string=""):
        self.body, self.err, self.status, self.error_string = body.encode(), error, status, error_string
        self.finished = Signal()
        self.aborted = False

    def readAll(self):
        return self.body

    def error(self):
        return self.err

    def errorString(self):
        return self.error_string

    def attribute(self, _attribute):
        return self.status

    def abort(self):
        self.aborted = True

    def deleteLater(self):
        pass


class Iface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.menu = []

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def addToolBar(self, name):
        return self.window.addToolBar(name)

    def addPluginToMenu(self, menu, action):
        self.menu.append(menu)

    def removePluginMenu(self, menu, action):
        self.menu.remove(menu)


iface = Iface()
plugin = GeoSearchRU.classFactory(iface)
plugin.initGui()
plugin.show_dialog()
dialog = plugin.dialog


def respond(reply):
    plugin.reply = reply
    plugin.handle_response()
    return dialog.status_label.text()


def test_menu():
    assert iface.menu == ["&Альтан-Эко"], iface.menu


def test_shared_toolbar():
    bar = iface.window.findChild(QToolBar, "AltanEcoToolbar")
    assert bar is not None and bar.windowTitle() == "Альтан-Эко"
    assert plugin.action in bar.actions()


def test_suggestions_list_and_marker():
    status = respond(FakeReply(json.dumps(SUGGESTIONS)))
    assert "Адрес и координаты найдены" in status, status
    labels = [dialog.results_list.item(i).text() for i in range(dialog.results_list.count())]
    assert labels == [
        "392000, Тамбовская обл, г Тамбов, пр-кт Энергетиков, д 7 — точные координаты дома",
        "г Тамбов — город",
        "г Тамбов, ул Несуществующая — без координат",
    ], labels
    assert "Точность: точные координаты дома" in dialog.normalized_address.toPlainText()
    x, y = plugin.marker.center().x(), plugin.marker.center().y()
    assert abs(x - 4614193) < 5 and abs(y - 6931373) < 5, (x, y)


def test_coarse_precision_warns():
    dialog.results_list.setCurrentRow(1)
    assert "приблизительные (город)" in dialog.status_label.text(), dialog.status_label.text()
    assert plugin.marker is not None


def test_no_coordinates_clears_marker():
    dialog.results_list.setCurrentRow(2)
    assert "нет координат" in dialog.status_label.text(), dialog.status_label.text()
    assert plugin.marker is None


def test_marker_follows_crs_change():
    dialog.results_list.setCurrentRow(0)
    iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    center = plugin.marker.center()
    assert abs(center.x() - 41.45) < 1e-6 and abs(center.y() - 52.72) < 1e-6, center.toString()
    iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))


def test_token_label_links_to_dadata():
    links = [label for label in dialog.findChildren(QLabel) if "dadata.ru/profile" in label.text()]
    assert len(links) == 1 and links[0].openExternalLinks(), [label.text() for label in dialog.findChildren(QLabel)]


def test_add_point_to_scratch_layer():
    dialog.results_list.setCurrentRow(2)
    assert not dialog.add_point_button.isEnabled(), "без координат добавлять нечего"
    dialog.results_list.setCurrentRow(0)
    assert dialog.add_point_button.isEnabled()
    dialog.add_point_button.click()
    layers = QgsProject.instance().mapLayersByName("Найденные адреса")
    assert len(layers) == 1 and layers[0].providerType() == "memory", layers
    layer = layers[0]
    assert layer.crs().authid() == "EPSG:4326" and layer.featureCount() == 1
    feature = next(layer.getFeatures())
    assert feature["address"] == "392000, Тамбовская обл, г Тамбов, пр-кт Энергетиков, д 7", feature.attributes()
    assert feature["precision"] == "точные координаты дома" and feature["qc_geo"] == 0, feature.attributes()
    point = feature.geometry().asPoint()
    assert abs(point.x() - 41.45) < 1e-9 and abs(point.y() - 52.72) < 1e-9, point.toString()
    assert layer.attributeDisplayName(layer.fields().indexOf("lat")) == "Широта"

    dialog.add_point_button.click()
    assert layer.featureCount() == 1 and "уже есть" in dialog.status_label.text(), dialog.status_label.text()

    dialog.results_list.setCurrentRow(1)
    dialog.add_point_button.click()
    assert layer.featureCount() == 2 and "точек: 2" in dialog.status_label.text(), dialog.status_label.text()

    QgsProject.instance().removeMapLayer(layer.id())
    dialog.results_list.setCurrentRow(0)
    dialog.add_point_button.click()
    layers = QgsProject.instance().mapLayersByName("Найденные адреса")
    assert len(layers) == 1 and layers[0].featureCount() == 1, "удалённый слой создаётся заново"


def test_clear_result():
    plugin.clear_result()
    assert plugin.marker is None and dialog.results_list.count() == 0


def test_empty_and_broken_answers():
    assert "Адрес не найден" in respond(FakeReply(json.dumps({"suggestions": []})))
    assert "Не удалось разобрать" in respond(FakeReply("<html>"))


def test_http_error_is_explained_and_escaped():
    body = '{"family":"CLIENT_ERROR","reason":"Forbidden","message":"Daily limit <exceeded>"}'
    status = respond(FakeReply(body, QNetworkReply.NetworkError.ContentAccessDenied, 403, "Forbidden"))
    assert "ошибку 403" in status and "дневной лимит" in status, status
    assert "&lt;exceeded&gt;" in status, status


def test_timeout_message():
    status = respond(FakeReply("", QNetworkReply.NetworkError.OperationCanceledError, None))
    assert "не ответил вовремя" in status, status


def test_token_storage_is_optional():
    dialog.remember_token.setChecked(True)
    plugin._save_token("secret")
    assert QSettings().value(plugin.SETTINGS_TOKEN) == "secret"
    dialog.remember_token.setChecked(False)
    plugin._save_token("secret")
    assert not QSettings().contains(plugin.SETTINGS_TOKEN)


def test_unload_aborts_request_silently():
    reply = FakeReply("{}")
    reply.finished.connect(plugin.handle_response)
    plugin.reply = reply
    plugin.unload()
    assert reply.aborted and reply.finished.slots == []
    assert plugin.dialog is None and plugin.action is None and iface.menu == []
    APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    bar = iface.window.findChild(QToolBar, "AltanEcoToolbar")
    assert bar is None, "пустая общая панель должна удаляться"


TESTS = [value for name, value in list(globals().items()) if name.startswith("test_")]
failed = 0
for test in TESTS:
    try:
        test()
        print(f"ok    {test.__name__}")
    except Exception:
        failed += 1
        print(f"FAIL  {test.__name__}")
        traceback.print_exc()
print(f"\n{len(TESTS) - failed} из {len(TESTS)} проверок прошли")
APP.exitQgis()
sys.exit(1 if failed else 0)
