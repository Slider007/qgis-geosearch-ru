import os

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsFeature, QgsGeometry,
    QgsMarkerSymbol, QgsNetworkAccessManager, QgsPointXY, QgsProject, QgsVectorLayer,
)
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtCore import QElapsedTimer, QSettings, QTimer
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from . import altan_toolbar
from .geosearch_ru_dialog import GeoSearchDialog
from .providers import Dadata, Nominatim, Yandex

POINTS_LAYER_NAME = "Найденные адреса"
POINTS_LAYER_URI = (
    "Point?crs=EPSG:4326&field=address:string&field=lat:double&field=lon:double"
    "&field=precision:string&field=source:string"
)
POINTS_FIELD_ALIASES = {
    "address": "Адрес", "lat": "Широта", "lon": "Долгота", "precision": "Точность", "source": "Источник",
}


class GeoSearchRU:
    SETTINGS_REMEMBER = "GeoSearchRU/remember_token"
    SETTINGS_PROVIDER = "GeoSearchRU/provider"
    RESULT_COUNT = 10
    TIMEOUT_MS = 15000

    def __init__(self, iface):
        self.iface, self.action, self.dialog = iface, None, None
        self.marker = self.marker_wgs84 = self.reply = None
        self.reply_key = None  # (provider id, normalised query) of the request in flight
        self.results = []
        self.points_layer_id = None
        self.providers = {p.ID: p for p in (Dadata(), Yandex(), Nominatim())}
        self.credentials = {}  # provider id → [key, secret] typed in the dialog
        self.shown_provider = None  # provider whose keys are in the dialog fields
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
        self.iface.mapCanvas().destinationCrsChanged.disconnect(self._reposition_marker)
        self._clear_marker()
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
            self.dialog = GeoSearchDialog([(p.ID, p.TITLE) for p in self.providers.values()], self.iface.mainWindow())
            settings = QSettings()
            remember = settings.value(self.SETTINGS_REMEMBER, True, type=bool)
            self.dialog.remember_token.setChecked(remember)
            for provider in self.providers.values():
                self.credentials[provider.ID] = [
                    settings.value(key, "", type=str) if remember and key else ""
                    for key in (provider.SETTINGS_TOKEN, provider.SETTINGS_SECRET)
                ]
            self.dialog.provider_combo.currentIndexChanged.connect(self._provider_changed)
            self.dialog.set_provider(settings.value(self.SETTINGS_PROVIDER, Dadata.ID, type=str))
            self._provider_changed()
            self.dialog.search_button.clicked.connect(self.search)
            self.dialog.results_list.currentRowChanged.connect(self.show_result)
            self.dialog.clear_button.clicked.connect(self.clear_result)
            self.dialog.add_point_button.clicked.connect(self.add_point)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def provider(self):
        return self.providers.get(self.dialog.provider_combo.currentData(), self.providers[Dadata.ID])

    def _provider_changed(self):
        if self.shown_provider:
            self.credentials[self.shown_provider] = [self.dialog.token_edit.text(), self.dialog.secret_edit.text()]
        provider = self.provider()
        self.shown_provider = provider.ID
        token, secret = self.credentials.get(provider.ID, ["", ""])
        self.dialog.token_edit.setText(token)
        self.dialog.secret_edit.setText(secret)
        QSettings().setValue(self.SETTINGS_PROVIDER, provider.ID)
        self.dialog.show_provider(provider)

    def search(self):
        provider = self.provider()
        address = self.dialog.address_edit.text().strip()
        token, secret = self.dialog.token_edit.text().strip(), self.dialog.secret_edit.text().strip()
        if not address:
            self.dialog.set_status("Введите адрес.", "error")
            return
        if provider.NEEDS_TOKEN:
            if not token:
                self.dialog.set_status(f"Введите: {provider.TOKEN_LABEL}.", "error")
                return
            self._save_credentials(provider, token, secret)
        self.results = []
        self.dialog.clear_results()
        key = (provider.ID, " ".join(address.lower().split()))
        if key in self.cache:
            self._show_results(provider, self.cache[key])
            return
        self.dialog.set_busy(True)
        self.pending = (provider, key, address, token, secret)
        timer = self.last_request.get(provider.ID)
        wait = provider.MIN_INTERVAL_MS - timer.elapsed() if timer else 0
        if wait > 0:
            self.throttle.start(wait)
        else:
            self._send_pending()

    def _send_pending(self):
        if self.pending is None:
            return
        provider, key, address, token, secret = self.pending
        self.pending = None
        request, body = provider.request(address, token, self.RESULT_COUNT, secret)
        request.setTransferTimeout(self.TIMEOUT_MS)
        timer = QElapsedTimer()
        timer.start()
        self.last_request[provider.ID] = timer
        # QGIS network manager honours the proxy and SSL settings configured in QGIS.
        manager = QgsNetworkAccessManager.instance()
        self.reply_key = key
        self.reply = manager.get(request) if body is None else manager.post(request, body)
        self.reply.finished.connect(self.handle_response)

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
                self.dialog.set_status(self._error_message(provider, reply, body), "error")
                return
        finally:
            reply.deleteLater()
        try:
            results = provider.parse(body)
        except (KeyError, TypeError, ValueError, AttributeError):
            self.dialog.set_status(f"Не удалось разобрать ответ {provider.SOURCE}.", "error")
            return
        self.cache[key] = results
        self._show_results(provider, results)

    def _show_results(self, provider, results):
        self.results = [(provider, result) for result in results]
        if not self.results:
            self.dialog.set_status("Адрес не найден.", "error")
            return
        self.dialog.set_results([f"{r.address} — {r.precision}" for r in results])

    def show_result(self, row):
        if not 0 <= row < len(self.results):
            return
        provider, result = self.results[row]
        self.dialog.add_point_button.setEnabled(result.latitude is not None)
        if result.latitude is None:
            self._clear_marker()
            self.dialog.normalized_address.setPlainText(f"{result.address}\n\nКоординаты не определены.")
            self.dialog.set_status("У этого варианта нет координат.", "error")
            return
        self.dialog.normalized_address.setPlainText(
            f"{result.address}\n\nКоординаты WGS 84:\nШирота: {result.latitude:.6f}\n"
            f"Долгота: {result.longitude:.6f}\nТочность: {result.precision}\nИсточник: {provider.SOURCE}"
        )
        if not self.center_and_mark(result.longitude, result.latitude, result.scale):
            self.dialog.set_status("Не удалось пересчитать координаты в систему координат проекта.", "error")
        elif result.coarse:
            self.dialog.set_status(f"Координаты приблизительные ({result.precision}). Уточните адрес.", "warning")
        else:
            self.dialog.set_status("Адрес и координаты найдены.")

    def add_point(self):
        row = self.dialog.results_list.currentRow()
        if not 0 <= row < len(self.results):
            return
        provider, result = self.results[row]
        if result.latitude is None:
            return
        layer = self._points_layer()
        for feature in layer.getFeatures():
            if (feature["address"], feature["lat"], feature["lon"]) == (result.address, result.latitude, result.longitude):
                self.dialog.set_status(f"Этот адрес уже есть в слое «{layer.name()}».", "warning")
                return
        feature = QgsFeature(layer.fields())
        feature.setAttributes([result.address, result.latitude, result.longitude, result.precision, provider.SOURCE])
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(result.longitude, result.latitude)))
        layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
        layer.triggerRepaint()
        self.dialog.set_status(f"Точка добавлена во временный слой «{layer.name()}» (точек: {layer.featureCount()}).")

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

    def center_and_mark(self, longitude, latitude, scale):
        self._clear_marker()
        wgs84 = QgsPointXY(longitude, latitude)
        point = self._to_canvas(wgs84)
        if point is None:
            return False
        canvas = self.iface.mapCanvas()
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

    def _save_credentials(self, provider, token, secret):
        settings = QSettings()
        remember = self.dialog.remember_token.isChecked()
        settings.setValue(self.SETTINGS_REMEMBER, remember)
        for key, value in ((provider.SETTINGS_TOKEN, token), (provider.SETTINGS_SECRET, secret)):
            if key and remember and value:
                settings.setValue(key, value)
            elif key:
                settings.remove(key)

    @staticmethod
    def _error_message(provider, reply, body):
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status is None:
            if reply.error() == QNetworkReply.NetworkError.OperationCanceledError:
                return (f"{provider.SOURCE} не ответил вовремя. "
                        "Проверьте подключение к интернету и настройки прокси в QGIS.")
            return f"Сетевая ошибка: {reply.errorString()}"
        message = f"{provider.SOURCE} вернул ошибку {status}: {provider.HTTP_HINTS.get(int(status), reply.errorString())}"
        detail = provider.error_detail(body)
        return f"{message} ({detail})" if detail else message
