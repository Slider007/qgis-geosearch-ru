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

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsProject  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtNetwork import QNetworkReply  # noqa: E402
from qgis.PyQt.QtWidgets import QLabel, QMainWindow, QMenu, QToolBar  # noqa: E402

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
    assert dialog.token_edit.isVisibleTo(dialog) and dialog.attribution_label.isHidden()


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
    assert feature["precision"] == "точные координаты дома" and feature["source"] == "DaData", feature.attributes()
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


NOMINATIM_ANSWER = [
    {"lat": "52.7200", "lon": "41.4500", "place_rank": 30, "display_name": "7, проспект Энергетиков, Тамбов, Россия",
     "address": {"house_number": "7", "road": "проспект Энергетиков"}},
    {"lat": "52.73", "lon": "41.44", "place_rank": 26, "display_name": "проспект Энергетиков, Тамбов, Россия",
     "address": {"road": "проспект Энергетиков"}},
    {"lat": "52.72", "lon": "41.45", "place_rank": 16, "display_name": "Тамбов, Россия", "address": {}},
]


class NominatimStub(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        NominatimStub.requests.append((time.monotonic(), self.path, dict(self.headers)))
        body = json.dumps(NOMINATIM_ANSWER).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


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
        assert not dialog.token_edit.isVisibleTo(dialog) and not dialog.attribution_label.isHidden()
        assert QSettings().value("GeoSearchRU/provider") == "nominatim"

        dialog.address_edit.setText("Тамбов, Энергетиков 7")
        dialog.search_button.click()
        wait_for_reply()
        assert not dialog.token_edit.isVisibleTo(dialog), "после поиска поле токена остаётся скрытым"
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
        query = parse_qs(urlparse(path).query)
        assert query["q"] == ["Тамбов, Энергетиков 7"] and query["format"] == ["jsonv2"], query
        assert query["countrycodes"] == ["ru"] and query["limit"] == ["10"], query
        assert "QGIS/" in headers.get("User-Agent", ""), headers.get("User-Agent")
        assert headers.get("Referer") == "https://github.com/Slider007/qgis-geosearch-ru", headers.get("Referer")

        dialog.address_edit.setText("  тамбов,  ЭНЕРГЕТИКОВ 7 ")
        dialog.search_button.click()
        assert len(NominatimStub.requests) == 1 and dialog.results_list.count() == 3, "повторный запрос — из кэша"

        dialog.address_edit.setText("Тамбов, Советская 1")
        dialog.search_button.click()
        wait_for_reply()
        assert len(NominatimStub.requests) == 2
        gap = NominatimStub.requests[1][0] - NominatimStub.requests[0][0]
        assert gap >= 0.99, f"между запросами к Nominatim меньше секунды: {gap:.3f} с"
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


def test_token_storage_is_optional():
    dadata = plugin.providers["dadata"]
    dialog.remember_token.setChecked(True)
    plugin._save_credentials(dadata, "secret", "")
    assert QSettings().value("GeoSearchRU/dadata_token") == "secret"
    dialog.remember_token.setChecked(False)
    plugin._save_credentials(dadata, "secret", "")
    assert not QSettings().contains("GeoSearchRU/dadata_token")


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


def test_yandex_keys_follow_the_source():
    dialog.set_provider("dadata")
    dialog.token_edit.setText("dadata-token")
    dialog.set_provider("yandex")
    assert dialog.token_edit.text() == "" and not dialog.secret_edit.isHidden()
    assert "developer.tech.yandex.ru" in dialog.token_label.text() and "Яндекс" in dialog.attribution_label.text()
    dialog.token_edit.setText("yandex-key")
    dialog.secret_edit.setText("yandex-secret")
    dialog.set_provider("dadata")
    assert dialog.token_edit.text() == "dadata-token" and dialog.secret_edit.isHidden()
    dialog.set_provider("yandex")
    assert (dialog.token_edit.text(), dialog.secret_edit.text()) == ("yandex-key", "yandex-secret")
    dialog.remember_token.setChecked(True)
    plugin._save_credentials(plugin.providers["yandex"], "yandex-key", "yandex-secret")
    assert QSettings().value("GeoSearchRU/yandex_key") == "yandex-key"
    assert QSettings().value("GeoSearchRU/yandex_secret") == "yandex-secret"
    assert QSettings().value("GeoSearchRU/dadata_token") != "yandex-key"
    dialog.set_provider("dadata")


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
