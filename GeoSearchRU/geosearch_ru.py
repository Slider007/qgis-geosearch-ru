import os

from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsFeature, QgsFillSymbol,
    QgsGeometry, QgsMarkerSymbol, QgsMessageLog, QgsNetworkAccessManager, QgsPointXY, QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsMapToolEmitPoint, QgsVertexMarker
from qgis.PyQt.QtCore import QElapsedTimer, QSettings, QTimer
from qgis.PyQt.QtGui import QColor, QIcon, QTextCursor
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import QDialog

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from . import altan_toolbar
from .geosearch_ru_dialog import GeoSearchDialog
from .keys_dialog import KeysDialog
from .nspd import FIELD_LABELS, FIELD_NAMES, NUMERIC_FIELDS, Nspd, is_cadastral_number, normalize_number
from .overpass import Overpass
from .providers import Dadata, Nominatim, PART_FIELDS, Yandex

POINTS_LAYER_NAME = "Найденные адреса"
POINTS_LAYER_URI = (
    "Point?crs=EPSG:4326&field=address:string&field=lat:double&field=lon:double"
    "&field=precision:string&field=source:string" + "".join(f"&field={name}:string" for name in PART_FIELDS)
)
POINTS_FIELD_ALIASES = {
    "address": "Адрес", "lat": "Широта", "lon": "Долгота", "precision": "Точность", "source": "Источник",
    "postcode": "Индекс", "region": "Регион", "area": "Район", "city": "Город или населённый пункт",
    "street": "Улица", "house": "Дом", "fias": "Код ФИАС", "oktmo": "ОКТМО", "okato": "ОКАТО",
}

CADASTRE_LAYER_NAME = "Кадастровые объекты"
CADASTRE_LAYER_URI = (
    "Polygon?crs=EPSG:4326&field=kind:string&field=source:string"
    + "".join(f"&field={name}:{'double' if name in NUMERIC_FIELDS else 'string'}" for name in FIELD_NAMES)
)
CADASTRE_FIELD_ALIASES = dict(FIELD_LABELS, kind="Тип объекта", source="Источник")


class GeoSearchRU:
    SETTINGS_REMEMBER = "GeoSearchRU/remember_token"
    SETTINGS_PROVIDER = "GeoSearchRU/provider"
    SETTINGS_BOUNDED = "GeoSearchRU/bounded"
    SETTINGS_INFO = "GeoSearchRU/what_is_here"  # кнопка «Что здесь?»: по умолчанию скрыта
    RESULT_COUNT = 10
    CHECK_QUERY = "Москва, Красная площадь, 1"  # one known address for the key check
    TIMEOUT_MS = 15000

    def __init__(self, iface):
        self.iface, self.action, self.dialog = iface, None, None
        self.marker = self.marker_wgs84 = self.reply = None
        self.reply_key = None  # (provider id, normalised query) of the request in flight
        self.ssl_problems = []  # претензии к сертификату сервера по текущему запросу
        self.current = None  # (провайдер, ключ, build) последнего запроса — для повтора
        self.attempt = 1
        self.map_tool = None  # «адрес по точке на карте» и «что здесь?»
        self.click_mode = "address"
        self.keep_view = False  # a reverse search leaves the map where the user clicked
        self.results = []
        self.points_layer_id = None
        self.cadastre_layer_ids = {}  # вид геометрии → id временного слоя с объектами ЕГРН
        self.own_manager = None  # для НСПД: там нужен свой User-Agent, см. _manager
        # Overpass отвечает на «Что здесь?»; в списке источников поиска адреса его нет.
        self.providers = {p.ID: p for p in (Dadata(), Yandex(), Nominatim(), Overpass(), Nspd())}
        self.credentials = {}  # provider id → [key, secret]
        self.keys_dialog = None
        self.check_reply = None
        self.cache = {}  # (provider id, query) → results; Nominatim policy asks to cache repeated queries
        self.last_request = {}  # provider id → QElapsedTimer since its last request
        self.pending = None  # search waiting for the provider's minimum interval
        self.throttle = QTimer()
        self.throttle.setSingleShot(True)
        self.throttle.timeout.connect(self._send_pending)

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "resources", "geosearch_ru.svg"))
        self.action = QAction(icon, "Поиск адреса…", self.iface.mainWindow())
        self.action.triggered.connect(self.show_dialog)
        altan_toolbar.add_action(self.iface, self.action)
        altan_toolbar.add_to_menu(self.iface, self.action)
        self.iface.mapCanvas().destinationCrsChanged.connect(self._reposition_marker)

    def unload(self):
        self.throttle.stop()
        self.pending = None
        self._abort_request()
        self._abort_check()
        if self.keys_dialog:
            self.keys_dialog.close()
            self.keys_dialog.deleteLater()
            self.keys_dialog = None
        self.iface.mapCanvas().destinationCrsChanged.disconnect(self._reposition_marker)
        if self.map_tool is not None:
            self.map_tool.deactivated.disconnect(self._tool_deactivated)
            self.iface.mapCanvas().unsetMapTool(self.map_tool)
            self.map_tool.deleteLater()
            self.map_tool = None
        self._clear_marker()
        if self.own_manager is not None:
            self.own_manager.deleteLater()
            self.own_manager = None
        if self.dialog:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        if self.action:
            altan_toolbar.remove_action(self.iface, self.action)
            altan_toolbar.remove_from_menu(self.iface, self.action)
            self.action.deleteLater()
            self.action = None

    def show_dialog(self):
        if self.dialog is None:
            searchable = [(p.ID, p.TITLE) for p in self.providers.values() if p.SEARCHABLE]
            self.dialog = GeoSearchDialog(searchable, self.iface.mainWindow())
            settings = QSettings()
            for provider in self.providers.values():
                self.credentials[provider.ID] = [
                    settings.value(key, "", type=str) if key else ""
                    for key in (provider.SETTINGS_TOKEN, provider.SETTINGS_SECRET)
                ]
            self._update_titles()
            self.dialog.provider_combo.currentIndexChanged.connect(self._provider_changed)
            self.dialog.set_provider(settings.value(self.SETTINGS_PROVIDER, Dadata.ID, type=str))
            self._provider_changed()
            self.dialog.keys_button.clicked.connect(lambda: self.open_keys(self.provider().ID))
            self.dialog.search_button.clicked.connect(self.search)
            self.dialog.cadastral_button.clicked.connect(self.search_cadastral)
            self.dialog.stop_button.clicked.connect(self.stop_request)
            self.dialog.results_list.currentRowChanged.connect(self.show_result)
            self.dialog.clear_button.clicked.connect(self.clear_result)
            self.dialog.add_point_button.clicked.connect(self.add_point)
            self.dialog.pick_button.toggled.connect(lambda on: self.set_click_mode("address", on))
            self.dialog.info_button.toggled.connect(lambda on: self.set_click_mode("info", on))
            if not settings.contains(self.SETTINGS_INFO):
                # Пишется в настройки, чтобы ключ был виден в «Параметры → Дополнительно».
                settings.setValue(self.SETTINGS_INFO, False)
            self.dialog.show_info_button(settings.value(self.SETTINGS_INFO, False, type=bool))
            remembered = settings.value(self.SETTINGS_BOUNDED, False, type=bool)
            self.dialog.remembered_bounded = remembered
            self.dialog.bounded.setChecked(remembered)
            self.dialog.bounded.toggled.connect(self._bounded_toggled)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def provider(self):
        return self.providers.get(self.dialog.provider_combo.currentData(), self.providers[Dadata.ID])

    def _has_key(self, provider):
        return not provider.NEEDS_TOKEN or bool(self.credentials.get(provider.ID, [""])[0])

    def _update_titles(self):
        self.dialog.set_provider_titles({
            p.ID: p.TITLE if self._has_key(p) else f"{p.TITLE} — нет ключа"
            for p in self.providers.values() if p.SEARCHABLE
        })

    def _bounded_toggled(self, checked):
        self.dialog.remembered_bounded = checked
        QSettings().setValue(self.SETTINGS_BOUNDED, checked)

    def _provider_changed(self):
        provider = self.provider()
        QSettings().setValue(self.SETTINGS_PROVIDER, provider.ID)
        self.dialog.show_provider(provider)

    def search(self):
        address = self.dialog.address_edit.text().strip()
        if not address:
            self.dialog.set_status("Введите адрес.", "error")
            return
        if is_cadastral_number(address):
            # Номер, набранный в поле адреса, тоже ищется в ЕГРН — чтобы не гадать, куда его вписывать.
            self.dialog.cadastral_edit.setText(address)
            self.search_cadastral()
            return
        provider = self.provider()
        bbox = self._map_bbox() if self.dialog.bounded.isChecked() and provider.SUPPORTS_BBOX else None
        key = (provider.ID, " ".join(address.lower().split()), bbox)
        self._start(provider, key,
                    lambda token, secret: provider.request(address, token, self.RESULT_COUNT, secret, None, bbox))

    def search_cadastral(self):
        """Кадастровый номер ищется в НСПД, какой бы источник ни был выбран."""
        text = self.dialog.cadastral_edit.text().strip()
        if not text:
            self.dialog.set_status("Введите кадастровый номер, например 68:29:0309001:24.", "error")
            return
        if not is_cadastral_number(text):
            self.dialog.set_status(
                "Это не похоже на кадастровый номер. Формат — 68:29:0309001:24 "
                "(можно и номер квартала: 68:29:0309001).", "error")
            return
        provider = self.providers[Nspd.ID]
        number = normalize_number(text)
        self._start(provider, (provider.ID, f"cadastral:{number}", None),
                    lambda token, secret: provider.request(number))

    def _start(self, provider, key, build):
        """Run one request, respecting the provider's minimum interval and the answer cache."""
        if not self._has_key(provider):
            self.dialog.set_status(f"Для источника «{provider.SOURCE}» нужен ключ — впишите его в окне «Ключи».", "error")
            self.open_keys(provider.ID)
            return
        self.results = []
        self.dialog.clear_results()
        if key in self.cache:
            self._show_results(provider, self.cache[key], key)
            return
        self.dialog.set_busy(True)
        self.attempt = 1
        self.pending = (provider, key, build)
        timer = self.last_request.get(provider.ID)
        wait = provider.MIN_INTERVAL_MS - timer.elapsed() if timer else 0
        if wait > 0:
            self.throttle.start(wait)
        else:
            self._send_pending()

    def _send_pending(self):
        if self.pending is None:
            return
        provider, key, build = self.pending
        self.pending = None
        self.current = (provider, key, build)
        token, secret = self.credentials.get(provider.ID, ["", ""])
        request, body = build(token, secret)
        request.setTransferTimeout(getattr(provider, "TIMEOUT_MS", self.TIMEOUT_MS))
        timer = QElapsedTimer()
        timer.start()
        self.last_request[provider.ID] = timer
        manager = self._manager(provider)
        self.reply_key = key
        self.ssl_problems = []
        self.reply = manager.get(request) if body is None else manager.post(request, body)
        # Qt сообщает SslHandshakeFailedError и когда сертификат не принят, и когда сервер
        # просто оборвал рукопожатие. Различить можно только по этому сигналу.
        self.reply.sslErrors.connect(self._ssl_errors)
        self.reply.finished.connect(self.handle_response)

    def _retry(self, provider, status):
        """Временный отказ службы — повторить, а не сдаваться.

        У НСПД защита от всплесков отвечает 597 и 429 и следом пропускает тот же
        запрос: одна попытка ничего не доказывает.
        """
        statuses = getattr(provider, "RETRY_STATUSES", ())
        attempts = getattr(provider, "MAX_ATTEMPTS", 1)
        if status is None or int(status) not in statuses or self.attempt >= attempts:
            return False
        if self.current is None:
            return False
        self.attempt += 1
        self.dialog.set_busy(True)
        self.dialog.set_status(
            f"{provider.SOURCE} отклонил запрос, пробую ещё раз "
            f"({self.attempt} из {attempts})…", "warning")
        self.pending = self.current
        self.throttle.start(getattr(provider, "RETRY_DELAY_MS", 3000))
        return True

    def stop_request(self):
        """Прервать запрос по кнопке «Остановить»: у НСПД с повторами ожидание долгое."""
        self.throttle.stop()
        self.pending = None
        self.current = None
        self._abort_request()
        self.reply_key = None
        self.dialog.set_busy(False)
        self.dialog.set_status("Поиск остановлен.", "warning")

    def _ssl_errors(self, errors):
        self.ssl_problems = [error.errorString() for error in errors]

    def _manager(self, provider):
        """Через какой сетевой менеджер слать запрос.

        Обычно — QGIS: он знает прокси и настройки SSL из параметров QGIS.
        Но QgsNetworkAccessManager подменяет User-Agent своим («Mozilla/5.0 QGIS/…»),
        а защита НСПД пропускает только браузерный. Поэтому у НСПД свой менеджер,
        на который переносится прокси QGIS — за прокси модуль тоже должен работать.
        """
        if not getattr(provider, "OWN_NETWORK", False):
            # QGIS network manager honours the proxy and SSL settings configured in QGIS.
            return QgsNetworkAccessManager.instance()
        if self.own_manager is None:
            self.own_manager = QNetworkAccessManager()
            self.own_manager.setProxy(QgsNetworkAccessManager.instance().proxy())
        return self.own_manager

    def handle_response(self):
        reply, self.reply = self.reply, None
        if reply is None:
            return
        self.dialog.set_busy(False)
        key, self.reply_key = self.reply_key, None
        provider = self.providers[key[0]]
        try:
            body = bytes(reply.readAll()).decode("utf-8", errors="replace")
            if reply.error() != QNetworkReply.NetworkError.NoError:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                if self._retry(provider, status):
                    return
                self.dialog.set_status(self._error_message(provider, reply, body), "error")
                return
        finally:
            reply.deleteLater()
        try:
            results = provider.parse(body)
        except (KeyError, TypeError, ValueError, AttributeError):
            self.dialog.set_status(
                f"Не удалось разобрать ответ {provider.SOURCE}: похоже, служба изменила формат. "
                "Повторите позже или выберите другой источник.", "error")
            return
        self.cache[key] = results
        self._show_results(provider, results, key)

    def _show_results(self, provider, results, key=None):
        self.results = [(provider, result) for result in results]
        self.dialog.show_attribution(provider)
        if not self.results:
            self.dialog.set_status(self._nothing_found(key), "error")
            return
        # Для объектов OSM вид уже стоит в начале строки — во второй раз его не повторяем.
        self.dialog.set_results([
            r.address if r.precision.lower() in r.address.lower() else f"{r.address} — {r.precision}"
            for r in results])

    @staticmethod
    def _nothing_found(key):
        if key and str(key[1]).startswith("info:"):
            return "В этой точке в OpenStreetMap ничего не нарисовано. Щёлкните по зданию или дороге."
        if key and str(key[1]).startswith("cadastral:"):
            number = str(key[1]).split(":", 1)[1]
            return f"В ЕГРН нет объекта с номером {number}. Проверьте номер."
        if key and str(key[1]).startswith("reverse:"):
            return "В этой точке адрес не найден. Щёлкните ближе к дому или улице."
        if key and len(key) > 2 and key[2]:
            return "Адрес не найден в пределах карты. Снимите галочку или сдвиньте карту."
        return "Адрес не найден."

    def show_result(self, row):
        if not 0 <= row < len(self.results):
            return
        provider, result = self.results[row]
        keep_view, self.keep_view = self.keep_view, False
        self.dialog.add_point_button.setEnabled(result.latitude is not None)
        self.dialog.show_add_mode(result.geometry is not None)
        self.dialog.normalized_address.setPlainText(self._card(provider, result))
        # Без этого карточка остаётся прокрученной с прошлого варианта и первые строки не видны.
        self.dialog.normalized_address.moveCursor(QTextCursor.MoveOperation.Start)
        if result.latitude is None:
            self._clear_marker()
            self.dialog.set_status(
                "Объект в ЕГРН есть, но границы не внесены — показывать на карте нечего."
                if provider.ID == Nspd.ID else "У этого варианта нет координат.", "error")
            return
        if not self.center_and_mark(result.longitude, result.latitude, result.scale, recenter=not keep_view):
            self.dialog.set_status(
                "Не удалось пересчитать координаты в систему координат проекта. "
                "Проверьте СК проекта в правом нижнем углу окна QGIS.", "error")
        elif provider.ID == Nspd.ID:
            self.dialog.set_status("Объект ЕГРН найден, границы показаны на карте.")
        elif not provider.COARSE_WARNING:
            self.dialog.set_status(f"Объект OpenStreetMap: {result.precision}.")
        elif result.coarse:
            self.dialog.set_status(f"Координаты приблизительные ({result.precision}). Уточните адрес.", "warning")
        else:
            self.dialog.set_status("Адрес и координаты найдены.")

    @staticmethod
    def _card(provider, result):
        """Карточка под списком: адрес, координаты, разбор адреса и сведения об объекте."""
        lines = [result.address, ""]
        if result.latitude is None:
            lines.append("Координаты не определены.")
        else:
            lines += ["Координаты WGS 84:", f"Широта: {result.latitude:.6f}", f"Долгота: {result.longitude:.6f}"]
        lines += [f"Точность: {result.precision}", f"Источник: {provider.SOURCE}"]
        parts = [f"{POINTS_FIELD_ALIASES[name]}: {value}" for name, value in (result.parts or {}).items()]
        if parts:
            lines += ["", "Разбор адреса:"] + parts
        if result.details:
            lines += ["", "Сведения:", result.details]
        return "\n".join(lines)

    def add_point(self):
        row = self.dialog.results_list.currentRow()
        if not 0 <= row < len(self.results):
            return
        provider, result = self.results[row]
        if result.latitude is None:
            return
        if result.geometry is not None:
            self._add_shape(provider, result)
            return
        layer = self._points_layer()
        for feature in layer.getFeatures():
            if (feature["address"], feature["lat"], feature["lon"]) == (result.address, result.latitude, result.longitude):
                self.dialog.set_status(f"Этот адрес уже есть в слое «{layer.name()}».", "warning")
                return
        feature = QgsFeature(layer.fields())
        values = dict(result.parts or {})
        values.update(address=result.address, lat=result.latitude, lon=result.longitude,
                      precision=result.precision, source=provider.SOURCE)
        for name, value in values.items():
            if layer.fields().indexOf(name) >= 0:
                feature[name] = value
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(result.longitude, result.latitude)))
        layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
        layer.triggerRepaint()
        self.dialog.set_status(f"Точка добавлена во временный слой «{layer.name()}» (точек: {layer.featureCount()}).")

    def _add_shape(self, provider, result):
        """Границы объекта ЕГРН во временный слой «Кадастровые объекты»."""
        wkt, values, kind = result.geometry
        geometry = QgsGeometry.fromWkt(wkt)  # WKT уже в WGS 84, как и слой
        if geometry.isNull() or geometry.isEmpty():
            self.dialog.set_status(
                "Границы объекта не удалось прочитать: в ответе ЕГРН неожиданная геометрия. "
                "Сведения об объекте остаются в карточке.", "error")
            return
        layer = self._cadastre_layer(geometry.type())
        number = values.get("cad_num", "")
        for feature in layer.getFeatures():
            if number and feature["cad_num"] == number:
                self.dialog.set_status(f"Этот объект уже есть в слое «{layer.name()}».", "warning")
                return
        feature = QgsFeature(layer.fields())
        for name, value in dict(values, kind=kind, source=provider.SOURCE).items():
            index = layer.fields().indexOf(name)
            if index < 0:
                continue
            if name in NUMERIC_FIELDS:
                try:
                    value = float(str(value).replace(",", ".").replace(" ", ""))
                except ValueError:
                    continue
            feature[name] = value
        feature.setGeometry(geometry)
        layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
        layer.triggerRepaint()
        self.dialog.set_status(
            f"Границы добавлены во временный слой «{layer.name()}» (объектов: {layer.featureCount()}).")

    def _cadastre_layer(self, geometry_type):
        """Слой объектов ЕГРН. Слоёв два: у ЕГРН есть и площадные объекты, и точечные."""
        point = geometry_type == Qgis.GeometryType.Point
        layer_id = self.cadastre_layer_ids.get(point)
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if layer is not None:
            return layer
        name = f"{CADASTRE_LAYER_NAME} (точки)" if point else CADASTRE_LAYER_NAME
        uri = CADASTRE_LAYER_URI.replace("Polygon?", "Point?" if point else "MultiPolygon?", 1)
        layer = QgsVectorLayer(uri, name, "memory")
        for field, alias in CADASTRE_FIELD_ALIASES.items():
            index = layer.fields().indexOf(field)
            if index >= 0:
                layer.setFieldAlias(index, alias)
        if point:
            layer.renderer().setSymbol(QgsMarkerSymbol.createSimple(
                {"name": "square", "color": "#1565c0", "outline_color": "#ffffff", "size": "3"}))
        else:
            layer.renderer().setSymbol(QgsFillSymbol.createSimple(
                {"color": "255,193,7,60", "outline_color": "#ff6f00", "outline_width": "0.6"}))
        QgsProject.instance().addMapLayer(layer)
        self.cadastre_layer_ids[point] = layer.id()
        return layer

    def _points_layer(self):
        # One scratch layer for all found addresses; recreated if the user removed it.
        layer = QgsProject.instance().mapLayer(self.points_layer_id) if self.points_layer_id else None
        if layer is None:
            layer = QgsVectorLayer(POINTS_LAYER_URI, POINTS_LAYER_NAME, "memory")
            for name, alias in POINTS_FIELD_ALIASES.items():
                layer.setFieldAlias(layer.fields().indexOf(name), alias)
            layer.renderer().setSymbol(QgsMarkerSymbol.createSimple(
                {"name": "circle", "color": "#d32f2f", "outline_color": "#ffffff", "size": "3"}
            ))
            QgsProject.instance().addMapLayer(layer)
            self.points_layer_id = layer.id()
        return layer

    def clear_result(self):
        self._clear_marker()
        self.results = []
        self.dialog.clear_results()
        self.dialog.status_label.clear()

    def center_and_mark(self, longitude, latitude, scale, recenter=True):
        self._clear_marker()
        wgs84 = QgsPointXY(longitude, latitude)
        point = self._to_canvas(wgs84)
        if point is None:
            return False
        canvas = self.iface.mapCanvas()
        if recenter:
            canvas.setCenter(point)
            canvas.zoomScale(scale)
        self.marker_wgs84 = wgs84
        self.marker = QgsVertexMarker(canvas)
        self.marker.setCenter(point)
        self.marker.setColor(QColor("#d32f2f"))
        self.marker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.marker.setIconSize(18)
        self.marker.setPenWidth(3)
        return True

    def set_click_mode(self, mode, enabled):
        """Два режима щелчка по карте: «address» — адрес по точке, «info» — что здесь есть."""
        canvas = self.iface.mapCanvas()
        if not enabled:
            if self.click_mode == mode and self.map_tool is not None and canvas.mapTool() is self.map_tool:
                canvas.unsetMapTool(self.map_tool)
            return
        self.click_mode = mode
        other = self.dialog.info_button if mode == "address" else self.dialog.pick_button
        other.setChecked(False)  # режимы взаимоисключающие
        if self.map_tool is None:
            self.map_tool = QgsMapToolEmitPoint(canvas)
            self.map_tool.canvasClicked.connect(self._point_picked)
            # The user can switch to another tool on the toolbar: the button has to follow.
            self.map_tool.deactivated.connect(self._tool_deactivated)
        canvas.setMapTool(self.map_tool)
        if mode == "info":
            self.dialog.set_status("Щёлкните по карте — модуль покажет, что здесь есть в OpenStreetMap.")
        else:
            self.dialog.set_status(f"Щёлкните по карте — {self.provider().SOURCE} найдёт ближайший адрес.")

    def _tool_deactivated(self):
        if self.dialog:
            self.dialog.pick_button.setChecked(False)
            self.dialog.info_button.setChecked(False)

    def _point_picked(self, point, _button=None):
        wgs84 = self._to_wgs84(point)
        if wgs84 is None:
            self.dialog.set_status(
                "Не удалось пересчитать точку карты в WGS 84. "
                "Проверьте СК проекта в правом нижнем углу окна QGIS.", "error")
            return
        info = self.click_mode == "info"
        provider = self.providers[Overpass.ID] if info else self.provider()
        latitude, longitude = round(wgs84.y(), 6), round(wgs84.x(), 6)
        key = (provider.ID, f"{'info' if info else 'reverse'}:{latitude},{longitude}")
        self.keep_view = True  # пользователь и так смотрит на это место
        self._start(provider, key,
                    lambda token, secret: provider.reverse(latitude, longitude, token, self.RESULT_COUNT, secret))

    def _map_bbox(self):
        """Visible map extent as (min lon, min lat, max lon, max lat) in WGS 84."""
        canvas = self.iface.mapCanvas()
        transform = QgsCoordinateTransform(
            canvas.mapSettings().destinationCrs(), QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
        try:
            extent = transform.transformBoundingBox(canvas.extent())
        except QgsCsException:
            return None
        if extent.isEmpty():
            return None
        return (round(extent.xMinimum(), 6), round(extent.yMinimum(), 6),
                round(extent.xMaximum(), 6), round(extent.yMaximum(), 6))

    def _to_wgs84(self, canvas_point):
        transform = QgsCoordinateTransform(
            self.iface.mapCanvas().mapSettings().destinationCrs(),
            QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
        try:
            return transform.transform(canvas_point)
        except QgsCsException:
            return None

    def _to_canvas(self, wgs84_point):
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), canvas_crs, QgsProject.instance())
        try:
            return transform.transform(wgs84_point)
        except QgsCsException:
            return None

    def _reposition_marker(self):
        # QgsVertexMarker stores map coordinates, so it has to follow project CRS changes.
        if not self.marker:
            return
        point = self._to_canvas(self.marker_wgs84)
        if point is None:
            self._clear_marker()
        else:
            self.marker.setCenter(point)

    def _clear_marker(self):
        if self.marker:
            self.iface.mapCanvas().scene().removeItem(self.marker)
        self.marker = self.marker_wgs84 = None

    def _abort_request(self):
        if self.reply:
            reply, self.reply = self.reply, None
            reply.finished.disconnect(self.handle_response)
            reply.abort()
            reply.deleteLater()

    def open_keys(self, provider_id=None):
        if self.keys_dialog is None:
            remember = QSettings().value(self.SETTINGS_REMEMBER, True, type=bool)
            keyed = [p for p in self.providers.values() if p.NEEDS_TOKEN]
            self.keys_dialog = KeysDialog(keyed, self.credentials, remember, self.dialog or self.iface.mainWindow())
            self.keys_dialog.check_requested.connect(self.check_key)
            # finished comes before accepted, so saving and cleanup live in one slot.
            self.keys_dialog.finished.connect(self._keys_closed)
        self.keys_dialog.focus(provider_id)
        self.keys_dialog.show()
        self.keys_dialog.raise_()
        self.keys_dialog.activateWindow()

    def _keys_closed(self, result):
        self._abort_check()
        keys_dialog, self.keys_dialog = self.keys_dialog, None
        if result == QDialog.DialogCode.Accepted:
            self.credentials.update(keys_dialog.credentials())
            self._save_credentials(keys_dialog.remember.isChecked())
            if self.dialog:
                self._update_titles()
                self.dialog.set_status("Ключи сохранены.")
        keys_dialog.deleteLater()

    def _save_credentials(self, remember):
        settings = QSettings()
        settings.setValue(self.SETTINGS_REMEMBER, remember)
        for provider in self.providers.values():
            values = self.credentials.get(provider.ID, ["", ""])
            for key, value in zip((provider.SETTINGS_TOKEN, provider.SETTINGS_SECRET), values):
                if key and remember and value:
                    settings.setValue(key, value)
                elif key:
                    settings.remove(key)

    def check_key(self, provider_id):
        """One test request with the keys typed in the «Ключи» window."""
        section = self.keys_dialog.sections[provider_id]
        token, secret = section.values()
        if not token:
            section.set_status(f"Впишите: {section.provider.TOKEN_LABEL}.", "error")
            return
        self._abort_check()
        modes = list(getattr(section.provider, "SIGNATURE_MODES", ())) if secret else []
        section.check_button.setEnabled(False)
        section.set_status("Проверяю…", "warning")
        self._send_check(section, token, secret, modes or [None], None)

    def _send_check(self, section, token, secret, modes, first_error):
        mode = modes.pop(0)
        request, body = section.provider.request(self.CHECK_QUERY, token, 1, secret, mode)
        request.setTransferTimeout(self.TIMEOUT_MS)
        manager = QgsNetworkAccessManager.instance()
        reply = manager.get(request) if body is None else manager.post(request, body)
        self.check_reply = reply
        reply.finished.connect(lambda: self._check_finished(reply, section, token, secret, mode, modes, first_error))

    def _check_finished(self, reply, section, token, secret, mode, modes, first_error):
        if reply is not self.check_reply:
            return  # aborted or superseded
        self.check_reply = None
        provider = section.provider
        body = bytes(reply.readAll()).decode("utf-8", errors="replace")
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        failed = reply.error() != QNetworkReply.NetworkError.NoError
        error = self._error_message(provider, reply, body) if failed else None
        reply.deleteLater()
        if failed and modes and status is not None and int(status) == 403:
            # Wrong signature variant — try the next one before reporting.
            self._send_check(section, token, secret, modes, first_error or error)
            return
        section.check_button.setEnabled(True)
        if failed:
            section.set_status(first_error or error, "error")
            return
        try:
            results = provider.parse(body)
        except (KeyError, TypeError, ValueError, AttributeError):
            section.set_status(f"Ключ принят, но ответ {provider.SOURCE} не удалось разобрать.", "error")
            return
        if mode:
            QSettings().setValue(provider.SETTINGS_SIGNATURE, mode)
        found = f": нашёлся «{results[0].address}»" if results else ""
        section.set_status(f"Работает{found}. Нажмите «Сохранить».")

    def _abort_check(self):
        if self.check_reply:
            reply, self.check_reply = self.check_reply, None
            reply.abort()
            reply.deleteLater()

    def _error_message(self, provider, reply, body):
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status is None:
            if reply.error() == QNetworkReply.NetworkError.SslHandshakeFailedError:
                if self.ssl_problems:
                    # Сертификат сервера и правда отвергнут: у НСПД он выдан Минцифры.
                    return (f"{provider.SOURCE}: сертификат сервера не принят ({self.ssl_problems[0]}). "
                            "Установите корневой сертификат Минцифры — «Настройки → Параметры → "
                            "Аутентификация → Центры сертификации».")
                # Претензий к сертификату не было: сервер оборвал рукопожатие, не показав сертификат.
                return (f"{provider.SOURCE} разорвал защищённое соединение. "
                        + (getattr(provider, "TIMEOUT_HINT", "") or "Проверьте подключение."))
            if reply.error() == QNetworkReply.NetworkError.OperationCanceledError:
                hint = getattr(provider, "TIMEOUT_HINT", None) or (
                    "Проверьте подключение к интернету и настройки прокси в QGIS.")
                return f"{provider.SOURCE} не ответил вовремя. {hint}"
            # errorString() у Qt по-английски: в русское окно его не выносим, он уходит в журнал.
            QgsMessageLog.logMessage(f"{provider.SOURCE}: {reply.errorString()}", "Поиск адреса", Qgis.Warning)
            return (f"{provider.SOURCE} недоступен. Проверьте подключение к интернету "
                    "и настройки прокси в QGIS; подробности — в журнале сообщений QGIS.")
        hint = provider.HTTP_HINTS.get(int(status))
        if hint is None:
            QgsMessageLog.logMessage(f"{provider.SOURCE} {status}: {reply.errorString()}", "Поиск адреса", Qgis.Warning)
            hint = "служба не приняла запрос, подробности — в журнале сообщений QGIS"
        message = f"{provider.SOURCE} вернул ошибку {status}: {hint}"
        detail = provider.error_detail(body)
        return f"{message} ({detail})" if detail else message
