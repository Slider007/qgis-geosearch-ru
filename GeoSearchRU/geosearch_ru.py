import json
import os

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsNetworkAccessManager,
    QgsPointXY, QgsProject,
)
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtCore import QSettings, QUrl
from qgis.PyQt.QtGui import QAction, QColor, QIcon
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from .geosearch_ru_dialog import GeoSearchDialog

# DaData qc_geo: how precisely the coordinates match the address.
QC_GEO_LABELS = {
    0: "точные координаты дома",
    1: "ближайший дом",
    2: "улица",
    3: "населённый пункт",
    4: "город",
}
QC_GEO_SCALES = {0: 2500, 1: 2500, 2: 10000, 3: 50000, 4: 100000}
COARSE_QC_GEO = 3
DEFAULT_SCALE = 50000

HTTP_ERROR_HINTS = {
    400: "некорректный запрос",
    401: "не указан API-токен",
    403: "неверный токен, не подтверждена почта или исчерпан дневной лимит",
    413: "слишком длинный запрос",
    429: "слишком много запросов, повторите позже",
}


class GeoSearchRU:
    API_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"
    SETTINGS_TOKEN = "GeoSearchRU/dadata_token"
    SETTINGS_REMEMBER = "GeoSearchRU/remember_token"
    MENU = "&Альтан-Эко"  # общее подменю модулей компании, строка должна совпадать буква в букву
    RESULT_COUNT = 10
    TIMEOUT_MS = 15000

    def __init__(self, iface):
        self.iface, self.action, self.dialog = iface, None, None
        self.marker = self.marker_wgs84 = self.reply = None
        self.suggestions = []

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "resources", "geosearch_ru.svg"))
        self.action = QAction(icon, "GeoSearch RU — найти адрес", self.iface.mainWindow())
        self.action.triggered.connect(self.show_dialog)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.MENU, self.action)
        self.iface.mapCanvas().destinationCrsChanged.connect(self._reposition_marker)

    def unload(self):
        self._abort_request()
        self.iface.mapCanvas().destinationCrsChanged.disconnect(self._reposition_marker)
        self._clear_marker()
        if self.dialog:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        if self.action:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu(self.MENU, self.action)
            self.action.deleteLater()
            self.action = None

    def show_dialog(self):
        if self.dialog is None:
            self.dialog = GeoSearchDialog(self.iface.mainWindow())
            settings = QSettings()
            remember = settings.value(self.SETTINGS_REMEMBER, True, type=bool)
            self.dialog.remember_token.setChecked(remember)
            if remember:
                self.dialog.token_edit.setText(settings.value(self.SETTINGS_TOKEN, "", type=str))
            self.dialog.search_button.clicked.connect(self.search)
            self.dialog.results_list.currentRowChanged.connect(self.show_suggestion)
            self.dialog.clear_button.clicked.connect(self.clear_result)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def search(self):
        address, token = self.dialog.address_edit.text().strip(), self.dialog.token_edit.text().strip()
        if not address or not token:
            self.dialog.set_status("Введите адрес и API-токен DaData.", "error")
            return
        self._save_token(token)
        request = QNetworkRequest(QUrl(self.API_URL))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setRawHeader(b"Accept", b"application/json")
        request.setRawHeader(b"Authorization", f"Token {token}".encode())
        request.setTransferTimeout(self.TIMEOUT_MS)
        self.suggestions = []
        self.dialog.clear_results()
        self.dialog.set_busy(True)
        body = json.dumps({"query": address, "count": self.RESULT_COUNT}).encode()
        # QGIS network manager honours the proxy and SSL settings configured in QGIS.
        self.reply = QgsNetworkAccessManager.instance().post(request, body)
        self.reply.finished.connect(self.handle_response)

    def handle_response(self):
        reply, self.reply = self.reply, None
        if reply is None:
            return
        self.dialog.set_busy(False)
        try:
            body = bytes(reply.readAll()).decode("utf-8", errors="replace")
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self.dialog.set_status(self._error_message(reply, body), "error")
                return
        finally:
            reply.deleteLater()
        try:
            suggestions = json.loads(body)["suggestions"]
        except (KeyError, TypeError, ValueError):
            suggestions = None
        if not isinstance(suggestions, list):
            self.dialog.set_status("Не удалось разобрать ответ DaData.", "error")
            return
        self.suggestions = [s for s in suggestions if isinstance(s, dict)]
        if not self.suggestions:
            self.dialog.set_status("Адрес не найден.", "error")
            return
        self.dialog.set_results([f"{self._address(s)} — {self._precision(s)}" for s in self.suggestions])

    def show_suggestion(self, row):
        if not 0 <= row < len(self.suggestions):
            return
        suggestion = self.suggestions[row]
        address, coordinates = self._address(suggestion), self._coordinates(suggestion)
        if coordinates is None:
            self._clear_marker()
            self.dialog.normalized_address.setPlainText(f"{address}\n\nКоординаты не определены.")
            self.dialog.set_status("У этого варианта нет координат.", "error")
            return
        latitude, longitude = coordinates
        qc_geo, precision = self._qc_geo(suggestion), self._precision(suggestion)
        self.dialog.normalized_address.setPlainText(
            f"{address}\n\nКоординаты WGS 84:\nШирота: {latitude:.6f}\nДолгота: {longitude:.6f}\nТочность: {precision}"
        )
        if not self.center_and_mark(longitude, latitude, QC_GEO_SCALES.get(qc_geo, DEFAULT_SCALE)):
            self.dialog.set_status("Не удалось пересчитать координаты в систему координат проекта.", "error")
        elif qc_geo is None or qc_geo >= COARSE_QC_GEO:
            self.dialog.set_status(f"Координаты приблизительные ({precision}). Уточните адрес.", "warning")
        else:
            self.dialog.set_status("Адрес и координаты найдены.")

    def clear_result(self):
        self._clear_marker()
        self.suggestions = []
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

    def _save_token(self, token):
        settings = QSettings()
        remember = self.dialog.remember_token.isChecked()
        settings.setValue(self.SETTINGS_REMEMBER, remember)
        if remember:
            settings.setValue(self.SETTINGS_TOKEN, token)
        else:
            settings.remove(self.SETTINGS_TOKEN)

    @staticmethod
    def _error_message(reply, body):
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status is None:
            if reply.error() == QNetworkReply.NetworkError.OperationCanceledError:
                return "DaData не ответил вовремя. Проверьте подключение к интернету и настройки прокси в QGIS."
            return f"Сетевая ошибка: {reply.errorString()}"
        message = f"DaData вернул ошибку {status}: {HTTP_ERROR_HINTS.get(int(status), reply.errorString())}"
        try:
            detail = json.loads(body).get("message")
        except (ValueError, AttributeError):
            detail = None
        return f"{message} ({detail})" if detail else message

    @staticmethod
    def _address(suggestion):
        return suggestion.get("unrestricted_value") or suggestion.get("value") or ""

    @staticmethod
    def _coordinates(suggestion):
        data = suggestion.get("data") or {}
        try:
            return float(data["geo_lat"]), float(data["geo_lon"])
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _qc_geo(suggestion):
        try:
            return int((suggestion.get("data") or {}).get("qc_geo"))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _precision(cls, suggestion):
        if cls._coordinates(suggestion) is None:
            return "без координат"
        return QC_GEO_LABELS.get(cls._qc_geo(suggestion), "точность неизвестна")
