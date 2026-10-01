"""Проверки плагина без интерфейса QGIS и без обращения к DaData. Запуск: tests/run_tests.sh

Ответы DaData подменяются поддельными, холст карты и окно плагина — настоящие.
Скрипт не трогает профиль QGIS: настройки пишутся во временный профиль tests/_profile.
"""

import json
import os
import shutil
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from qgis.PyQt.QtCore import QElapsedTimer, QEvent, QCoreApplication, QSettings  # noqa: E402

PROFILE = os.path.join(HERE, "_profile")
shutil.rmtree(PROFILE, ignore_errors=True)
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
QCoreApplication.setOrganizationName("geosearch-ru-tests")
QCoreApplication.setApplicationName("geosearch-ru-tests")

from qgis.core import (  # noqa: E402
    NULL, Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsNetworkAccessManager, QgsPointXY,
    QgsProject, QgsRectangle,
)
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtNetwork import QNetworkReply  # noqa: E402
from qgis.PyQt.QtWidgets import QDialogButtonBox, QLabel, QMainWindow, QMenu, QToolBar  # noqa: E402

APP = QgsApplication([], True)
APP.initQgis()
# initQgis() moves settings into ~/Library/Application Support/<organisation>/…/profiles/default,
# which is never cleaned up: put them back into the throwaway profile.
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
assert QSettings().fileName().startswith(PROFILE), QSettings().fileName()

import GeoSearchRU  # noqa: E402

NO_ERROR = QNetworkReply.NetworkError.NoError

SUGGESTIONS = {"suggestions": [
    {"value": "г Тамбов, пр-кт Энергетиков, д 7",
     "unrestricted_value": "392000, Тамбовская обл, г Тамбов, пр-кт Энергетиков, д 7",
     "data": {"geo_lat": "52.72", "geo_lon": "41.45", "qc_geo": "0", "postal_code": "392000",
              "region_with_type": "Тамбовская обл", "city_with_type": "г Тамбов",
              "street_with_type": "пр-кт Энергетиков", "house": "7", "block": "к 1",
              "fias_id": "1b0b3f5a-0000-4000-8000-000000000001", "oktmo": "68701000001", "okato": "68401000000"}},
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
        self.plugin_menu = QMenu("Модули")

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def addToolBar(self, name):
        return self.window.addToolBar(name)

    def pluginMenu(self):
        return self.plugin_menu

    def addPluginToMenu(self, menu, action):
        self.menu.append(menu)

    def removePluginMenu(self, menu, action):
        self.menu.remove(menu)


iface = Iface()
plugin = GeoSearchRU.classFactory(iface)
plugin.initGui()
plugin.show_dialog()
dialog = plugin.dialog


def respond(reply, key=("dadata", "запрос")):
    plugin.reply, plugin.reply_key = reply, key
    plugin.handle_response()
    return dialog.status_label.text()


def test_dadata_is_default_source():
    assert dialog.provider_combo.currentData() == "dadata"
    assert [dialog.provider_combo.itemData(i) for i in range(dialog.provider_combo.count())] == [
        "dadata", "yandex", "nominatim"]
    assert dialog.attribution_label.isHidden() and dialog.keys_button.isVisibleTo(dialog)
    titles = [dialog.provider_combo.itemText(i) for i in range(dialog.provider_combo.count())]
    assert titles == ["DaData — нет ключа", "Яндекс — нет ключа", "OpenStreetMap (без ключа)"], titles


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


def keys_window():
    return plugin.keys_dialog


def save_keys():
    keys_window().findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
    APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_search_without_key_opens_keys_window():
    dialog.set_provider("yandex")
    dialog.address_edit.setText("Тамбов")
    dialog.search_button.click()
    assert "нужен ключ" in dialog.status_label.text(), dialog.status_label.text()
    assert keys_window() is not None and keys_window().isVisible()
    keys_window().reject()
    APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert plugin.keys_dialog is None
    dialog.set_provider("dadata")


def test_keys_window_links_to_where_keys_are():
    plugin.open_keys("dadata")
    links = [label.text() for label in keys_window().findChildren(QLabel) if "href" in label.text()]
    assert any("dadata.ru/profile" in link for link in links), links
    assert any("developer.tech.yandex.ru" in link for link in links), links
    assert all(label.openExternalLinks() for label in keys_window().findChildren(QLabel) if "href" in label.text())
    assert set(keys_window().sections) == {"dadata", "yandex"}, "OpenStreetMap ключ не нужен"
    assert not keys_window().sections["dadata"].secret_edit.isVisibleTo(keys_window())
    assert keys_window().sections["yandex"].secret_edit.isVisibleTo(keys_window())
    keys_window().reject()
    APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_keys_are_saved_from_keys_window():
    plugin.open_keys("dadata")
    sections = keys_window().sections
    sections["dadata"].token_edit.setText("dadata-token")
    sections["yandex"].token_edit.setText("yandex-key")
    sections["yandex"].secret_edit.setText("yandex-secret")
    keys_window().remember.setChecked(True)
    save_keys()
    assert plugin.keys_dialog is None
    settings = QSettings()
    assert settings.value("GeoSearchRU/dadata_token") == "dadata-token"
    assert (settings.value("GeoSearchRU/yandex_key"), settings.value("GeoSearchRU/yandex_secret")) == (
        "yandex-key", "yandex-secret")
    titles = [dialog.provider_combo.itemText(i) for i in range(dialog.provider_combo.count())]
    assert titles == ["DaData", "Яндекс", "OpenStreetMap (без ключа)"], titles
    assert "Ключи сохранены" in dialog.status_label.text()

    plugin.open_keys("yandex")
    assert keys_window().sections["yandex"].values() == ("yandex-key", "yandex-secret"), "ключи подставлены"
    keys_window().reject()
    APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)


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
    assert feature["precision"] == "точные координаты дома" and feature["source"] == "DaData", feature.attributes()
    point = feature.geometry().asPoint()
    assert abs(point.x() - 41.45) < 1e-9 and abs(point.y() - 52.72) < 1e-9, point.toString()
    assert layer.attributeDisplayName(layer.fields().indexOf("lat")) == "Широта"

    parts = {name: feature[name] for name in ("postcode", "region", "city", "street", "house", "oktmo", "okato")}
    assert parts == {"postcode": "392000", "region": "Тамбовская обл", "city": "г Тамбов",
                     "street": "пр-кт Энергетиков", "house": "7 к 1", "oktmo": "68701000001",
                     "okato": "68401000000"}, parts
    assert feature["fias"] == "1b0b3f5a-0000-4000-8000-000000000001"
    assert layer.attributeDisplayName(layer.fields().indexOf("oktmo")) == "ОКТМО"

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


NOMINATIM_ANSWER = [
    {"lat": "52.7200", "lon": "41.4500", "place_rank": 30, "display_name": "7, проспект Энергетиков, Тамбов, Россия",
     "address": {"house_number": "7", "road": "проспект Энергетиков"}},
    {"lat": "52.73", "lon": "41.44", "place_rank": 26, "display_name": "проспект Энергетиков, Тамбов, Россия",
     "address": {"road": "проспект Энергетиков"}},
    {"lat": "52.72", "lon": "41.45", "place_rank": 16, "display_name": "Тамбов, Россия", "address": {}},
]


NOMINATIM_REVERSE = {
    "lat": "52.7201", "lon": "41.4502", "place_rank": 30,
    "display_name": "7, проспект Энергетиков, Тамбов, 392000, Россия",
    "address": {"house_number": "7", "road": "проспект Энергетиков", "city": "Тамбов",
                "state": "Тамбовская область", "postcode": "392000"},
}


class NominatimStub(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        NominatimStub.requests.append((time.monotonic(), self.path, dict(self.headers)))
        # /reverse answers with one place, /search with a list.
        answer = NOMINATIM_REVERSE if "/reverse" in self.path else NOMINATIM_ANSWER
        body = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def lower_headers(raw):
    """Имена заголовков без учёта регистра: Qt6 шлёт Sec-Ch-Ua-Mobile, Qt5 — sec-ch-ua-mobile."""
    return {name.lower(): value for name, value in raw.items()}


def wait_for_reply(timeout_ms=10000):
    timer = QElapsedTimer()
    timer.start()
    while (plugin.reply is not None or plugin.pending is not None) and timer.elapsed() < timeout_ms:
        APP.processEvents()
        time.sleep(0.01)
    assert plugin.reply is None and plugin.pending is None, "нет ответа от тестового сервера"


def test_nominatim_end_to_end():
    server = HTTPServer(("127.0.0.1", 0), NominatimStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    QSettings().setValue("GeoSearchRU/nominatim_url", f"http://127.0.0.1:{server.server_port}/search")
    try:
        dialog.set_provider("nominatim")
        assert not dialog.attribution_label.isHidden()
        assert QSettings().value("GeoSearchRU/provider") == "nominatim"

        dialog.address_edit.setText("Тамбов, Энергетиков 7")
        dialog.search_button.click()
        wait_for_reply()
        labels = [dialog.results_list.item(i).text() for i in range(dialog.results_list.count())]
        assert labels == [
            "7, проспект Энергетиков, Тамбов, Россия — дом",
            "проспект Энергетиков, Тамбов, Россия — улица",
            "Тамбов, Россия — город или крупнее",
        ], labels
        assert "Источник: OpenStreetMap" in dialog.normalized_address.toPlainText()
        assert "Адрес и координаты найдены" in dialog.status_label.text(), dialog.status_label.text()
        dialog.results_list.setCurrentRow(2)
        assert "приблизительные" in dialog.status_label.text(), dialog.status_label.text()

        assert len(NominatimStub.requests) == 1
        _, path, headers = NominatimStub.requests[0]
        headers = lower_headers(headers)
        query = parse_qs(urlparse(path).query)
        assert query["q"] == ["Тамбов, Энергетиков 7"] and query["format"] == ["jsonv2"], query
        assert query["countrycodes"] == ["ru"] and query["limit"] == ["10"], query
        assert "QGIS/" in headers.get("user-agent", ""), headers.get("user-agent")
        assert headers.get("referer") == "https://github.com/Slider007/qgis-geosearch-ru", headers.get("referer")

        dialog.address_edit.setText("  тамбов,  ЭНЕРГЕТИКОВ 7 ")
        dialog.search_button.click()
        assert len(NominatimStub.requests) == 1 and dialog.results_list.count() == 3, "повторный запрос — из кэша"

        dialog.address_edit.setText("Тамбов, Советская 1")
        dialog.search_button.click()
        wait_for_reply()
        assert len(NominatimStub.requests) == 2
        gap = NominatimStub.requests[1][0] - NominatimStub.requests[0][0]
        # Пауза меряется QElapsedTimer, у него погрешность в единицы миллисекунд:
        # проверяем, что выдержана секунда, а не что таймер идеально точен.
        assert gap >= 0.95, f"между запросами к Nominatim меньше секунды: {gap:.3f} с"
    finally:
        server.shutdown()
        dialog.set_provider("dadata")


def test_nominatim_url_setting_is_visible():
    from GeoSearchRU.providers import Nominatim
    QSettings().remove("GeoSearchRU/nominatim_url")
    assert Nominatim().url() == "https://nominatim.openstreetmap.org/search"
    assert QSettings().value("GeoSearchRU/nominatim_url") == "https://nominatim.openstreetmap.org/search"
    QSettings().setValue("GeoSearchRU/nominatim_url", "  ")
    assert Nominatim().url() == "https://nominatim.openstreetmap.org/search", "пустое значение — публичный сервер"


def test_nominatim_precision_levels():
    from GeoSearchRU.providers import Nominatim
    parse = Nominatim._parse_one
    assert parse({"lat": "1", "lon": "2", "place_rank": 22, "address": {"house_number": "5"}}).precision == "дом"
    village = parse({"lat": "1", "lon": "2", "place_rank": 19})
    assert village.precision == "населённый пункт или район" and village.coarse and village.scale == 50000
    assert parse({"place_rank": 30}).latitude is None


def nominatim_server():
    server = HTTPServer(("127.0.0.1", 0), NominatimStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    QSettings().setValue("GeoSearchRU/nominatim_url", f"http://127.0.0.1:{server.server_port}/search")
    NominatimStub.requests = []
    return server


def last_query():
    return parse_qs(urlparse(NominatimStub.requests[-1][1]).query)


def test_map_frame_limits_search():
    server = nominatim_server()
    try:
        dialog.set_provider("dadata")
        assert not dialog.bounded.isEnabled(), "DaData рамку карты не умеет"
        dialog.set_provider("nominatim")
        assert dialog.bounded.isEnabled()
        iface.canvas.setExtent(QgsRectangle(4613000, 6930000, 4615000, 6932000))
        dialog.bounded.setChecked(True)
        assert QSettings().value("GeoSearchRU/bounded", type=bool), "галочка запоминается"
        dialog.address_edit.setText("Энергетиков 7")
        dialog.search_button.click()
        wait_for_reply()
        query = last_query()
        assert query["bounded"] == ["1"], query
        box = [float(v) for v in query["viewbox"][0].split(",")]
        assert 41.3 < box[0] < 41.5 and 52.6 < box[1] < 52.8, box
        assert box[0] < box[2] and box[1] < box[3], box

        dialog.bounded.setChecked(False)
        dialog.address_edit.setText("Энергетиков 7")
        dialog.search_button.click()
        wait_for_reply()
        assert "viewbox" not in last_query(), "без галочки рамка не отправляется"
    finally:
        dialog.bounded.setChecked(False)
        server.shutdown()
        dialog.set_provider("dadata")


def test_address_by_map_click():
    server = nominatim_server()
    try:
        dialog.set_provider("nominatim")
        iface.canvas.setExtent(QgsRectangle(4613000, 6930000, 4615000, 6932000))
        scale, center = iface.canvas.scale(), iface.canvas.center()
        dialog.pick_button.setChecked(True)
        assert iface.canvas.mapTool() is plugin.map_tool, "карте выдан инструмент выбора точки"

        plugin._point_picked(QgsPointXY(4614193, 6931373))
        wait_for_reply()
        query = last_query()
        assert "/reverse?" in NominatimStub.requests[-1][1], NominatimStub.requests[-1][1]
        assert abs(float(query["lat"][0]) - 52.72) < 0.01 and abs(float(query["lon"][0]) - 41.45) < 0.01, query
        labels = [dialog.results_list.item(i).text() for i in range(dialog.results_list.count())]
        assert labels == ["7, проспект Энергетиков, Тамбов, 392000, Россия — дом"], labels
        assert plugin.marker is not None
        assert abs(iface.canvas.scale() - scale) < 1e-6 and iface.canvas.center() == center, "карта не прыгает"

        dialog.add_point_button.click()
        layer = QgsProject.instance().mapLayersByName("Найденные адреса")[0]
        feature = sorted(layer.getFeatures(), key=lambda f: f["source"])[-1]
        assert feature["source"] == "OpenStreetMap", feature["source"]
        assert (feature["street"], feature["house"], feature["city"], feature["postcode"]) == (
            "проспект Энергетиков", "7", "Тамбов", "392000"), feature.attributes()
        assert feature["oktmo"] is None or feature["oktmo"] == NULL, "у OSM кодов нет"

        dialog.pick_button.setChecked(False)
        assert iface.canvas.mapTool() is not plugin.map_tool, "инструмент отпускается"
    finally:
        server.shutdown()
        dialog.set_provider("dadata")


OVERPASS_ANSWER = {"elements": [
    {"type": "node", "id": 1, "lat": 52.7201, "lon": 41.4502},  # вершина линии, без тегов
    {"type": "node", "id": 2, "lat": 52.7202, "lon": 41.4503, "tags": {"name": "Спутник", "shop": "travel_agency"}},
    {"type": "way", "id": 3, "center": {"lat": 52.72005, "lon": 41.45005},
     "tags": {"building": "commercial", "building:levels": "1", "addr:street": "Интернациональная улица",
              "addr:housenumber": "30Г", "addr:city": "Тамбов", "addr:postcode": "392033"}},
    {"type": "way", "id": 4, "center": {"lat": 52.7203, "lon": 41.4499},
     "tags": {"power": "line", "voltage": "110000", "cables": "3", "operator": "Россети"}},
    {"type": "way", "id": 5, "center": {"lat": 52.7199, "lon": 41.4505},
     "tags": {"highway": "service", "surface": "asphalt"}},
    {"type": "area", "id": 6, "tags": {"boundary": "administrative", "admin_level": "8", "name": "Тамбов"}},
    {"type": "area", "id": 7, "tags": {"boundary": "administrative", "admin_level": "4",
                                       "name": "Тамбовская область"}},
    {"type": "area", "id": 8, "tags": {"boundary": "economic", "name": "Центрально-Чернозёмный район"}},
]}


class OverpassStub(BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        OverpassStub.bodies.append((self.rfile.read(length).decode(), dict(self.headers)))
        body = json.dumps(OVERPASS_ANSWER).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_what_is_here():
    from urllib.parse import unquote
    server = HTTPServer(("127.0.0.1", 0), OverpassStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    QSettings().setValue("GeoSearchRU/overpass_url", f"http://127.0.0.1:{server.server_port}/api/interpreter")
    OverpassStub.bodies = []
    try:
        dialog.set_provider("dadata")  # источник поиска роли не играет
        dialog.show_info_button(True)  # по умолчанию кнопка скрыта, см. test_info_button_hidden_by_default
        dialog.info_button.setChecked(True)
        assert plugin.click_mode == "info" and iface.canvas.mapTool() is plugin.map_tool
        dialog.pick_button.setChecked(True)
        assert not dialog.info_button.isChecked() and plugin.click_mode == "address", "режимы взаимоисключающие"
        dialog.info_button.setChecked(True)

        iface.canvas.setExtent(QgsRectangle(4613000, 6930000, 4615000, 6932000))
        scale = iface.canvas.scale()
        plugin._point_picked(QgsPointXY(4614193, 6931373))
        wait_for_reply()

        query, headers = OverpassStub.bodies[-1]
        headers = lower_headers(headers)
        query = unquote(query[len("data="):])
        assert query.startswith("[out:json][timeout:25];is_in(52.7") and query.endswith("out tags center 60;"), query
        assert "way(around:30,52.7" in query and "node(around:30,52.7" in query, query
        assert headers.get("referer") == "https://github.com/Slider007/qgis-geosearch-ru"

        labels = [dialog.results_list.item(i).text() for i in range(dialog.results_list.count())]
        assert labels == [
            "Здание: коммерческое, Интернациональная улица, 30Г",
            "Электросети: ЛЭП",
            "Дорога: проезд",
            "Магазин: турагентство, «Спутник»",
            "Граница: город или поселение, «Тамбов»",
            "Граница: субъект, «Тамбовская область»",
        ], labels
        assert "экономический" not in " ".join(labels), "лишние площади из is_in отброшены"
        assert abs(iface.canvas.scale() - scale) < 1e-6, "карта не прыгает"
        assert not dialog.attribution_label.isHidden() and "OpenStreetMap" in dialog.attribution_label.text()

        card = dialog.normalized_address.toPlainText()
        assert "Этажей: 1" in card and "Индекс: 392033" in card and "Улица: Интернациональная улица" in card, card
        dialog.results_list.setCurrentRow(1)
        card = dialog.normalized_address.toPlainText()
        assert "Напряжение, В: 110000" in card and "Обслуживает: Россети" in card, card
        assert "power=line" not in card, "вид объекта не повторяется тегом"
        assert "Объект OpenStreetMap" in dialog.status_label.text(), dialog.status_label.text()

        dialog.results_list.setCurrentRow(0)
        dialog.add_point_button.click()
        layer = QgsProject.instance().mapLayersByName("Найденные адреса")[0]
        feature = sorted(layer.getFeatures(), key=lambda f: f.id())[-1]
        assert (feature["house"], feature["postcode"]) == ("30Г", "392033"), feature.attributes()

        dialog.info_button.setChecked(False)
        assert iface.canvas.mapTool() is not plugin.map_tool
    finally:
        dialog.show_info_button(False)
        server.shutdown()


def test_info_button_hidden_by_default():
    assert not dialog.info_button.isVisibleTo(dialog), "«Что здесь?» скрыта, пока не включена настройкой"
    assert QSettings().value("GeoSearchRU/what_is_here", type=bool) is False, "ключ виден в настройках QGIS"
    dialog.show_info_button(True)
    dialog.info_button.setChecked(True)
    dialog.show_info_button(False)
    assert not dialog.info_button.isChecked() and iface.canvas.mapTool() is not plugin.map_tool, "режим снимается"


def test_what_is_here_empty_and_busy():
    from GeoSearchRU.overpass import Overpass
    assert Overpass().parse('{"elements": []}') == []
    assert Overpass().error_detail('{"remark": "runtime error: Query timed out"}') == "runtime error: Query timed out"
    assert plugin._nothing_found(("overpass", "info:52.72,41.45")).startswith("В этой точке в OpenStreetMap")


def test_reverse_answer_without_address():
    from GeoSearchRU.providers import Nominatim
    assert Nominatim().parse('{"error": "Unable to geocode"}') == []
    QSettings().remove("GeoSearchRU/nominatim_url")  # предыдущая проверка оставила тестовый сервер
    assert Nominatim().url("reverse") == "https://nominatim.openstreetmap.org/reverse"
    QSettings().setValue("GeoSearchRU/nominatim_url", "https://nominatim.example.ru/search")
    assert Nominatim().url("reverse") == "https://nominatim.example.ru/reverse"


def test_dadata_reverse_request():
    from GeoSearchRU.providers import Dadata
    request, body = Dadata().reverse(52.72, 41.45, "TOKEN", 10)
    assert request.url().toString().endswith("/rs/geolocate/address"), request.url().toString()
    payload = json.loads(body)
    assert payload == {"lat": 52.72, "lon": 41.45, "count": 10, "radius_meters": 1000}, payload
    assert bytes(request.rawHeader(b"Authorization")) == b"Token TOKEN"


def test_keys_not_stored_when_unticked():
    plugin.open_keys("dadata")
    keys_window().remember.setChecked(False)
    save_keys()
    assert not QSettings().contains("GeoSearchRU/dadata_token") and not QSettings().contains("GeoSearchRU/yandex_key")
    assert plugin.credentials["dadata"][0] == "dadata-token", "до закрытия QGIS ключ остаётся в памяти"
    assert dialog.provider_combo.itemText(0) == "DaData"
    plugin.open_keys("dadata")
    keys_window().remember.setChecked(True)
    save_keys()


YANDEX_ANSWER = {"response": {"GeoObjectCollection": {
    "metaDataProperty": {"GeocoderResponseMetaData": {"request": "Тамбов, Энергетиков 7", "found": "2"}},
    "featureMember": [
        {"GeoObject": {
            "metaDataProperty": {"GeocoderMetaData": {
                "kind": "house", "precision": "exact", "text": "Россия, Тамбов, проспект Энергетиков, 7",
                "Address": {"country_code": "RU", "postal_code": "392000", "formatted": "Россия, Тамбов, проспект Энергетиков, 7"}}},
            "name": "проспект Энергетиков, 7", "Point": {"pos": "41.450000 52.720000"}}},
        {"GeoObject": {
            "metaDataProperty": {"GeocoderMetaData": {"kind": "locality", "precision": "other", "text": "Россия, Тамбов"}},
            "name": "Тамбов", "Point": {"pos": "41.45 52.72"}}},
    ]}}}


class ScriptedStub(BaseHTTPRequestHandler):
    """Answers requests with the next (status, body) from `script`, records method and path."""
    script = []
    seen = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        ScriptedStub.seen.append((self.command, self.path))
        status, body = ScriptedStub.script.pop(0)
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


def run_check(provider_id, host_attr, token, secret, script):
    server = HTTPServer(("127.0.0.1", 0), ScriptedStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ScriptedStub.script, ScriptedStub.seen = list(script), []
    provider = plugin.providers[provider_id]
    old = getattr(provider, host_attr)
    base = f"http://127.0.0.1:{server.server_port}"
    setattr(provider, host_attr, base if host_attr == "HOST" else base + "/suggest")
    try:
        plugin.open_keys(provider_id)
        section = keys_window().sections[provider_id]
        section.token_edit.setText(token)
        section.secret_edit.setText(secret)
        section.check_button.click()
        timer = QElapsedTimer()
        timer.start()
        while not section.check_button.isEnabled() and timer.elapsed() < 10000:
            APP.processEvents()
            time.sleep(0.01)
        return section.status_label.text(), list(ScriptedStub.seen)
    finally:
        setattr(provider, host_attr, old)
        server.shutdown()


def test_key_check_finds_working_yandex_signature():
    QSettings().remove("GeoSearchRU/yandex_signature")
    status, seen = run_check("yandex", "HOST", "KEY", "c2VjcmV0", [
        (403, '{"statusCode":403,"error":"Forbidden","message":"Invalid signature"}'),
        (200, json.dumps(YANDEX_ANSWER)),
    ])
    assert "Работает" in status and "392000" in status, status
    assert len(seen) == 2 and all("&signature=" in path for _, path in seen), seen
    assert seen[0][1] != seen[1][1], "второй запрос — с другой подписью"
    assert QSettings().value("GeoSearchRU/yandex_signature") == "plain", "запомнен способ подписи, который сработал"
    keys_window().reject()
    APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_key_check_reports_bad_key():
    status, seen = run_check("yandex", "HOST", "BAD", "", [
        (403, '{"statusCode":403,"error":"Forbidden","message":"Invalid apikey"}'),
    ])
    assert len(seen) == 1 and "signature" not in seen[0][1], seen
    assert "ошибку 403" in status and "Invalid apikey" in status, status
    keys_window().reject()
    APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_key_check_dadata():
    status, seen = run_check("dadata", "URL", "TOKEN", "", [(200, json.dumps(SUGGESTIONS))])
    assert seen and seen[0][0] == "POST", seen
    assert "Работает" in status and "Энергетиков" in status, status
    keys_window().reject()
    APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_yandex_answer():
    dialog.set_provider("yandex")
    try:
        status = respond(FakeReply(json.dumps(YANDEX_ANSWER)), key=("yandex", "тамбов"))
        assert "Адрес и координаты найдены" in status, status
        labels = [dialog.results_list.item(i).text() for i in range(dialog.results_list.count())]
        assert labels == [
            "392000, Россия, Тамбов, проспект Энергетиков, 7 — точные координаты дома",
            "Россия, Тамбов — населённый пункт",
        ], labels
        x, y = plugin.marker.center().x(), plugin.marker.center().y()
        assert abs(x - 4614193) < 5 and abs(y - 6931373) < 5, (x, y)
        assert "Источник: Яндекс" in dialog.normalized_address.toPlainText()
        dialog.results_list.setCurrentRow(1)
        assert "приблизительные" in dialog.status_label.text(), dialog.status_label.text()
        status = respond(FakeReply('{"statusCode":403,"error":"Forbidden","message":"Invalid apikey"}',
                                   QNetworkReply.NetworkError.ContentAccessDenied, 403), key=("yandex", "x"))
        assert "Яндекс вернул ошибку 403" in status and "Invalid apikey" in status, status
    finally:
        dialog.set_provider("dadata")


def test_yandex_request_and_signature():
    import base64
    import hashlib
    import hmac
    from datetime import datetime, timezone
    from GeoSearchRU.providers import Yandex

    QSettings().remove("GeoSearchRU/yandex_signature")  # default: secret and time
    request, body = Yandex().request("Тамбов, Энергетиков 7", "KEY", 10)
    url = bytes(request.url().toEncoded()).decode()
    assert body is None and "signature" not in url, url
    assert url.startswith("https://geocode-maps.yandex.ru/v1/?geocode=%D0%A2") and "&apikey=KEY" in url, url

    secret = base64.urlsafe_b64encode(b"0123456789abcdef").decode().rstrip("=")
    request, _ = Yandex().request("Тамбов", "KEY", 10, secret)
    url = bytes(request.url().toEncoded()).decode()
    path, signature = url[len("https://geocode-maps.yandex.ru"):].split("&signature=")
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    temporal = hmac.new(b"0123456789abcdef", hour.encode(), hashlib.sha256).digest()
    expected = base64.urlsafe_b64encode(hmac.new(temporal, path.encode(), hashlib.sha256).digest()).decode()
    assert signature == expected, (signature, expected)
    plain = Yandex.signature(path, secret, "plain")
    assert plain == base64.urlsafe_b64encode(hmac.new(b"0123456789abcdef", path.encode(), hashlib.sha256).digest()).decode()


def test_yandex_map_frame_and_reverse():
    from urllib.parse import unquote
    from GeoSearchRU.providers import Yandex

    request, _ = Yandex().request("Энергетиков 7", "KEY", 10, bbox=(41.43, 52.70, 41.45, 52.71))
    query = unquote(bytes(request.url().toEncoded()).decode())
    assert "bbox=41.430000,52.700000~41.450000,52.710000" in query, query
    assert "rspn=1" in query and query.endswith("&apikey=KEY"), query

    request, body = Yandex().reverse(52.72, 41.45, "KEY", 10)
    query = unquote(bytes(request.url().toEncoded()).decode())
    assert body is None and "geocode=41.450000,52.720000" in query, query


def test_yandex_address_parts():
    from GeoSearchRU.providers import Yandex
    answer = json.loads(json.dumps(YANDEX_ANSWER))
    block = answer["response"]["GeoObjectCollection"]["featureMember"][0]["GeoObject"]
    block["metaDataProperty"]["GeocoderMetaData"]["Address"]["Components"] = [
        {"kind": "country", "name": "Россия"},
        {"kind": "province", "name": "Центральный федеральный округ"},
        {"kind": "province", "name": "Тамбовская область"},
        {"kind": "locality", "name": "Тамбов"},
        {"kind": "street", "name": "проспект Энергетиков"},
        {"kind": "house", "name": "7"},
    ]
    parts = Yandex().parse(json.dumps(answer))[0].parts
    assert parts == {"postcode": "392000", "region": "Тамбовская область", "city": "Тамбов",
                     "street": "проспект Энергетиков", "house": "7"}, parts


# --- НСПД: поиск объекта ЕГРН по кадастровому номеру ---------------------------------

NSPD_ANSWER = {"data": {"type": "FeatureCollection", "features": [
    {"id": 274180162, "type": "Feature",
     "geometry": {"type": "Polygon", "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
                  "coordinates": [[[4614084.51030582, 6925812.158053609],
                                   [4614086.305353552, 6925845.364793987],
                                   [4614056.110684374, 6925847.825804596],
                                   [4614054.3157130275, 6925814.718070663],
                                   [4614084.51030582, 6925812.158053609]]]},
     "properties": {"category": 36368, "categoryName": "Земельные участки ЕГРН",
                    "descr": "68:29:0309001:24", "label": "68:29:0309001:24",
                    "options": {"area": 1000.5, "cad_num": "68:29:0309001:24",
                                "cost_determination_date": "2022-01-01", "cost_value": 1282227.6,
                                "land_record_category_type": "Земли населенных пунктов",
                                "land_record_reg_date": "2003-03-06",
                                "land_record_subtype": "Землепользование",
                                "land_record_type": "Земельный участок", "ownership_type": None,
                                "permitted_use_established_by_document": "Под жилой   дом",
                                "quarter_cad_number": "68:29:0309001",
                                "readable_address": "Тамбовская обл, г Тамбов, ул Мичуринская"}}}]}}


def to_mercator(longitude, latitude):
    """WGS 84 → EPSG:3857, чтобы проверять пересчёт в обратную сторону."""
    import math
    x = math.radians(longitude) * 6378137.0
    y = 6378137.0 * math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2))
    return x, y


class NspdStub(BaseHTTPRequestHandler):
    requests = []
    answer = NSPD_ANSWER
    statuses = []  # коды, которые сервер отдаст перед успешным ответом

    def do_GET(self):
        NspdStub.requests.append((self.path, dict(self.headers)))
        if NspdStub.statuses:
            code = NspdStub.statuses.pop(0)
            body = b"Forbidden"
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(NspdStub.answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_cadastral_number_recognised():
    from GeoSearchRU.nspd import is_cadastral_number, normalize_number
    for text in ("68:29:0309001:24", "50:12:0000000:123", "68:29:0309001", " 68 : 29 : 0309001 : 24 "):
        assert is_cadastral_number(text), text
    for text in ("Тамбов, Энергетиков 7", "68:29", "", "68-29-0309001-24", "улица 1:2:3:4я"):
        assert not is_cadastral_number(text), text
    assert normalize_number(" 68 : 29 : 0309001 : 24 ") == "68:29:0309001:24"


def test_nspd_geometry_is_converted_to_wgs84():
    from GeoSearchRU.nspd import geometry_wkt
    corners = [(41.45, 52.72), (41.46, 52.72), (41.46, 52.73), (41.45, 52.73)]
    mercator = [list(to_mercator(lon, lat)) for lon, lat in corners]
    wkt, box = geometry_wkt({"type": "Polygon", "coordinates": [mercator]})
    assert wkt.startswith("POLYGON(("), wkt
    assert wkt.count(",") == 4, "WKT замыкает кольцо пятой точкой"
    assert len(box) == 4, "в рамку объекта замыкающая вершина не попадает"
    for (want_lon, want_lat), (got_lon, got_lat) in zip(corners, box):
        assert abs(got_lon - want_lon) < 1e-7 and abs(got_lat - want_lat) < 1e-7, (got_lon, got_lat)
    assert geometry_wkt(None) == (None, [])
    assert geometry_wkt({"type": "Polygon", "coordinates": []}) == (None, [])


def test_nspd_parses_object():
    from GeoSearchRU.nspd import Nspd
    result = Nspd().parse(json.dumps(NSPD_ANSWER))[0]
    assert result.address == "68:29:0309001:24 — Земельные участки ЕГРН, Тамбовская обл, г Тамбов, ул Мичуринская"
    assert result.precision == "границы по ЕГРН" and result.coarse is False
    assert abs(result.latitude - 52.65) < 0.05 and abs(result.longitude - 41.45) < 0.05, result
    wkt, values, kind = result.geometry
    assert wkt.startswith("POLYGON((") and kind == "Земельные участки ЕГРН"
    assert values["cad_num"] == "68:29:0309001:24" and values["area"] == "1000.5"
    assert values["use"] == "Под жилой дом", values["use"]  # лишние пробелы убираются
    assert values["cost"] == "1282227.6" and values["quarter"] == "68:29:0309001"
    assert "Категория земель: Земли населенных пунктов" in result.details
    assert "Кадастровый номер" not in result.details, "номер уже в заголовке"
    assert result.scale and result.scale < 5000, result.scale


def test_nspd_object_without_boundaries():
    from GeoSearchRU.nspd import Nspd
    answer = json.loads(json.dumps(NSPD_ANSWER))
    answer["data"]["features"][0]["geometry"] = None
    result = Nspd().parse(json.dumps(answer))[0]
    assert result.latitude is None and result.geometry is None
    assert result.precision == "без границ в ЕГРН"
    assert "Категория земель" in result.details, "сведения показываются и без границ"


def test_nspd_end_to_end():
    from GeoSearchRU.nspd import Nspd
    server = HTTPServer(("127.0.0.1", 0), NspdStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    QSettings().setValue("GeoSearchRU/nspd_url", f"http://127.0.0.1:{server.server_port}/api/search")
    NspdStub.requests = []
    try:
        dialog.set_provider("dadata")  # источник адресов остаётся выбранным
        dialog.cadastral_edit.setText(" 68:29:0309001:24 ")
        dialog.cadastral_button.click()
        wait_for_reply()

        path, headers = NspdStub.requests[0]
        headers = lower_headers(headers)
        query = parse_qs(urlparse(path).query)
        assert query["query"] == ["68:29:0309001:24"] and query["thematicSearchId"] == ["1"], query
        assert headers.get("referer") == "https://nspd.gov.ru/map?thematic=PKK", headers.get("referer")
        # Защита НСПД пропускает только браузерный User-Agent, поэтому свой менеджер сети.
        assert "QGIS/" not in headers.get("user-agent", ""), headers.get("user-agent")
        assert headers.get("user-agent") == Nspd.USER_AGENT, headers.get("user-agent")
        assert headers.get("sec-fetch-site") == "same-origin"
        # Без набора sec-ch-ua защита НСПД отвечает 597: проверено на живом сервере.
        assert headers.get("sec-ch-ua-mobile") == "?0", headers
        assert headers.get("sec-ch-ua-platform") == '"macOS"', headers
        assert "Chromium" in headers.get("sec-ch-ua", ""), headers
        assert "Chrome/140" in headers.get("sec-ch-ua", "") + headers.get("user-agent", "")

        assert dialog.results_list.count() == 1
        card = dialog.normalized_address.toPlainText()
        assert "Источник: НСПД (ЕГРН)" in card and "Разрешённое использование: Под жилой дом" in card, card
        assert "Кадастровая стоимость, ₽: 1282227.6" in card, card
        assert dialog.add_point_button.text() == "Добавить границы во временный слой"
        assert dialog.add_point_button.isEnabled()

        dialog.add_point_button.click()
        layers = [l for l in QgsProject.instance().mapLayers().values() if l.name() == "Кадастровые объекты"]
        assert len(layers) == 1, "слой границ один"
        layer = layers[0]
        assert layer.crs().authid() == "EPSG:4326" and layer.featureCount() == 1
        feature = next(layer.getFeatures())
        assert feature["cad_num"] == "68:29:0309001:24" and feature["kind"] == "Земельные участки ЕГРН"
        assert abs(feature["area"] - 1000.5) < 1e-6 and abs(feature["cost"] - 1282227.6) < 1e-3
        assert feature["source"] == "НСПД (ЕГРН)"
        assert feature.geometry().type() == Qgis.GeometryType.Polygon
        index = layer.fields().indexOf("cad_num")
        assert layer.attributeAlias(index) == "Кадастровый номер", layer.attributeAlias(index)

        dialog.add_point_button.click()
        assert layer.featureCount() == 1 and "уже есть" in dialog.status_label.text()

        dialog.cadastral_edit.setText("68:29:0309001:24")
        dialog.cadastral_button.click()
        assert len(NspdStub.requests) == 1, "повторный номер — из кэша"
    finally:
        server.shutdown()
        QgsProject.instance().removeAllMapLayers()
        plugin.cadastre_layer_ids = {}
        plugin.points_layer_id = None
        plugin.cache.clear()
        QSettings().remove("GeoSearchRU/nspd_url")


def test_nspd_ssl_error_explains_certificate():
    error = QNetworkReply.NetworkError.SslHandshakeFailedError
    key = ("nspd", "cadastral:68:29:0309001:24", None)
    plugin.ssl_problems = ["The root certificate of the certificate chain is self-signed"]
    message = respond(FakeReply("", error=error, status=None, error_string="SSL handshake failed"), key=key)
    assert "сертификат сервера не принят" in message and "Минцифры" in message, message
    assert "self-signed" in message, message
    # Сервер оборвал рукопожатие сам — сертификат тут ни при чём, писать про него нельзя.
    plugin.ssl_problems = []
    message = respond(FakeReply("", error=error, status=None, error_string="SSL handshake failed"), key=key)
    assert "разорвал защищённое соединение" in message, message
    assert "сертификат" not in message.lower(), message


def test_nspd_retries_after_temporary_refusal():
    """597 у НСПД — отбой всплеска, а не окончательный отказ: запрос надо повторить."""
    from GeoSearchRU.nspd import Nspd
    source = plugin.providers[Nspd.ID]
    server = HTTPServer(("127.0.0.1", 0), NspdStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    QSettings().setValue("GeoSearchRU/nspd_url", f"http://127.0.0.1:{server.server_port}/api/search")
    NspdStub.requests, NspdStub.statuses = [], [597, 597]
    delay, source.RETRY_DELAY_MS = source.RETRY_DELAY_MS, 50
    try:
        dialog.cadastral_edit.setText("68:29:0309001:24")
        dialog.cadastral_button.click()
        wait_for_reply()
        assert len(NspdStub.requests) == 3, f"должно быть три попытки, а было {len(NspdStub.requests)}"
        assert dialog.results_list.count() == 1, "после повтора объект должен найтись"
        assert "отклонил" not in dialog.status_label.text(), dialog.status_label.text()

        # Отказы не кончились — тогда сдаёмся и объясняем.
        NspdStub.requests, NspdStub.statuses = [], [597, 597, 597]
        plugin.cache.clear()
        dialog.cadastral_button.click()
        wait_for_reply()
        assert len(NspdStub.requests) == 3, "больше трёх попыток не делаем"
        assert "597" in dialog.status_label.text(), dialog.status_label.text()
    finally:
        source.RETRY_DELAY_MS = delay
        NspdStub.statuses = []
        server.shutdown()
        QgsProject.instance().removeAllMapLayers()
        plugin.cadastre_layer_ids, plugin.points_layer_id = {}, None
        plugin.cache.clear()
        QSettings().remove("GeoSearchRU/nspd_url")


def test_map_frame_choice_survives_source_switch():
    """У DaData рамка не работает: галочка гаснет, но выбор пользователя не теряется."""
    dialog.set_provider("nominatim")
    dialog.bounded.setChecked(True)
    assert QSettings().value("GeoSearchRU/bounded", False, type=bool) is True
    dialog.set_provider("dadata")
    assert not dialog.bounded.isEnabled() and not dialog.bounded.isChecked(), "у DaData галочка гаснет"
    assert QSettings().value("GeoSearchRU/bounded", False, type=bool) is True, "выбор не должен стираться"
    dialog.set_provider("nominatim")
    assert dialog.bounded.isChecked(), "при возврате к источнику с рамкой галочка возвращается"
    dialog.bounded.setChecked(False)
    dialog.set_provider("dadata")


def test_bottom_buttons_fit_at_large_font():
    """Подписи внизу обрезались при крупном системном шрифте: ширина окна должна расти вместе с ним."""
    from qgis.PyQt.QtGui import QFont
    from qgis.PyQt.QtWidgets import QPushButton
    from GeoSearchRU.geosearch_ru_dialog import ADD_SHAPE_TEXT, GeoSearchDialog

    normal = iface.window.font()
    big = QFont(normal)
    big.setPointSizeF(max(normal.pointSizeF(), 10.0) * 1.5)
    try:
        iface.window.setFont(big)  # окно создаётся уже с крупным шрифтом
        probe = GeoSearchDialog([("dadata", "DaData")], iface.window)
        probe.show_add_mode(True)  # самая длинная подпись
        names = ("Очистить маркер", "Адрес по точке на карте", ADD_SHAPE_TEXT, "Закрыть")
        row = [b for b in probe.findChildren(QPushButton) if b.text() in names]
        assert len(row) == len(names), [b.text() for b in row]
        needed = sum(b.sizeHint().width() for b in row) + 8 * len(row)  # запас на промежутки
        assert needed <= probe.minimumWidth(), (
            f"при крупном шрифте кнопкам нужно {needed} px, а окно не шире {probe.minimumWidth()} px")
        probe.deleteLater()
    finally:
        iface.window.setFont(normal)


def test_stop_button_cancels_request():
    """Запрос к НСПД с тремя попытками длится до минуты: должна быть кнопка выхода."""
    assert not dialog.stop_button.isVisibleTo(dialog), "в покое кнопки «Остановить» не видно"
    reply = FakeReply("{}")
    reply.finished.connect(plugin.handle_response)
    plugin.reply, plugin.reply_key = reply, ("nspd", "cadastral:68:29:0309001:24", None)
    plugin.current = (plugin.providers["nspd"], plugin.reply_key, lambda t, s: (None, None))
    dialog.set_busy(True)
    assert dialog.stop_button.isVisibleTo(dialog), "во время запроса кнопка видна"
    dialog.stop_button.click()
    assert reply.aborted and reply.finished.slots == [], "ответ оборван, обработчик отключён"
    assert plugin.reply is None and plugin.pending is None and plugin.current is None
    assert "остановлен" in dialog.status_label.text(), dialog.status_label.text()
    assert not dialog.stop_button.isVisibleTo(dialog), "после остановки кнопка прячется"
    assert dialog.search_button.isEnabled(), "поля снова доступны"


def test_network_error_is_russian():
    """errorString() у Qt по-английски — в русское окно он попадать не должен."""
    message = respond(FakeReply("", error=QNetworkReply.NetworkError.HostNotFoundError, status=None,
                                error_string="Host nspd.gov.ru not found"),
                      key=("nspd", "cadastral:68:29:0309001:24", None))
    assert "Host" not in message and "not found" not in message, message
    assert "недоступен" in message and "журнале сообщений QGIS" in message, message
    # Незнакомый код HTTP тоже не должен тащить английский текст в окно.
    message = respond(FakeReply("", error=QNetworkReply.NetworkError.UnknownContentError, status=418,
                                error_string="I am a teapot"),
                      key=("nspd", "cadastral:68:29:0309001:24", None))
    assert "teapot" not in message, message
    assert "418" in message, message


def test_failure_messages_say_what_to_do():
    """Сообщение об ошибке без выхода оставляет человека в тупике."""
    message = respond(FakeReply("не json"), key=("nspd", "cadastral:68:29:0309001:24", None))
    assert "Повторите позже" in message or "другой источник" in message, message


def test_nspd_url_with_parameters():
    """Свой адрес сервера может уже содержать «?» — склейка строк ломала запрос."""
    from GeoSearchRU.nspd import Nspd
    QSettings().setValue("GeoSearchRU/nspd_url", "https://nspd.example.ru/api/search?token=abc")
    try:
        request, _body = Nspd().request("68:29:0309001:24")
        url = request.url()
        assert url.toString().count("?") == 1, url.toString()
        query = parse_qs(urlparse(url.toString()).query)
        assert query["token"] == ["abc"], query
        assert query["query"] == ["68:29:0309001:24"] and query["thematicSearchId"] == ["1"], query
    finally:
        QSettings().remove("GeoSearchRU/nspd_url")


def test_busy_and_empty_texts():
    """Мелочи, которые видит человек: «ё» в слове «идёт» и пояснение у пустого списка."""
    dialog.set_busy(True)
    assert "Идёт поиск" in dialog.status_label.text(), dialog.status_label.text()
    dialog.set_busy(False)
    dialog.clear_results()
    assert "здесь появятся" in dialog.results_label.text(), dialog.results_label.text()


def test_status_colours_follow_theme():
    """На тёмном фоне тёмно-красный не читается: цвета состояния идут за палитрой."""
    from qgis.PyQt.QtGui import QColor, QPalette
    from GeoSearchRU.geosearch_ru_dialog import status_colour

    light, dark = QPalette(), QPalette()
    light.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
    dark.setColor(QPalette.ColorRole.Window, QColor(45, 45, 48))
    label = QLabel()
    label.setPalette(light)
    assert status_colour(label, "error") == "#b71c1c", status_colour(label, "error")
    label.setPalette(dark)
    for level in ("ok", "warning", "error"):
        colour = QColor(status_colour(label, level))
        assert colour.lightness() > 150, f"{level}: {colour.name()} на тёмном фоне не прочитать"


def test_cadastral_field_checks_input():
    """Отдельное поле КН: пустое и непохожее на номер объясняются, запроса не будет."""
    dialog.cadastral_edit.clear()
    dialog.cadastral_button.click()
    assert "Введите кадастровый номер" in dialog.status_label.text(), dialog.status_label.text()
    assert plugin.reply is None and plugin.pending is None, "запроса быть не должно"
    dialog.cadastral_edit.setText("Тамбов, Энергетиков 7")
    dialog.cadastral_button.click()
    assert "не похоже на кадастровый номер" in dialog.status_label.text(), dialog.status_label.text()
    assert plugin.reply is None and plugin.pending is None, "запроса быть не должно"
    dialog.cadastral_edit.clear()


def test_cadastral_number_in_address_field_still_works():
    """Номер, набранный в поле адреса, не теряется: перекладывается в поле КН."""
    server = HTTPServer(("127.0.0.1", 0), NspdStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    QSettings().setValue("GeoSearchRU/nspd_url", f"http://127.0.0.1:{server.server_port}/api/search")
    NspdStub.requests = []
    try:
        dialog.cadastral_edit.clear()
        dialog.address_edit.setText("68:29:0309001:24")
        dialog.search_button.click()
        wait_for_reply()
        assert dialog.cadastral_edit.text() == "68:29:0309001:24", dialog.cadastral_edit.text()
        assert len(NspdStub.requests) == 1, "должен уйти запрос в НСПД, а не в DaData"
        assert dialog.results_list.count() == 1
    finally:
        server.shutdown()
        QgsProject.instance().removeAllMapLayers()
        plugin.cadastre_layer_ids, plugin.points_layer_id = {}, None
        plugin.cache.clear()
        dialog.address_edit.clear()
        dialog.cadastral_edit.clear()
        QSettings().remove("GeoSearchRU/nspd_url")


def test_nspd_uses_its_own_network_manager():
    from GeoSearchRU.nspd import Nspd
    from GeoSearchRU.providers import Nominatim
    assert plugin._manager(Nominatim()) is QgsNetworkAccessManager.instance()
    own = plugin._manager(Nspd())
    assert own is not QgsNetworkAccessManager.instance()
    assert plugin._manager(Nspd()) is own, "менеджер создаётся один раз"


def test_nspd_url_setting_is_visible():
    from GeoSearchRU.nspd import Nspd
    QSettings().remove("GeoSearchRU/nspd_url")
    assert Nspd().url() == Nspd.DEFAULT_URL
    assert QSettings().value("GeoSearchRU/nspd_url") == Nspd.DEFAULT_URL
    QSettings().remove("GeoSearchRU/nspd_url")


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
# The profile folder initQgis() created for the test organisation (qgis.db, styles) — only ours.
LEFTOVER = os.path.dirname(os.path.dirname(os.path.dirname(QgsApplication.qgisSettingsDirPath().rstrip("/"))))
APP.exitQgis()
if os.path.basename(LEFTOVER) == "geosearch-ru-tests":
    shutil.rmtree(LEFTOVER, ignore_errors=True)
sys.exit(1 if failed else 0)
