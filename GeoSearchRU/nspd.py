"""Поиск объекта ЕГРН по кадастровому номеру на геопортале НСПД.

Отвечает не адресом, а объектом недвижимости с границами: участок, здание,
сооружение. Границы приходят полигоном в EPSG:3857 — здесь они пересчитываются
в WGS 84 и отдаются готовым WKT, чтобы разбор ответа не зависел от QGIS.

Про сеть: НСПД закрыт защитой от автоматических обращений, которая смотрит на
заголовки браузера. QgsNetworkAccessManager подменяет User-Agent своим
(«Mozilla/5.0 QGIS/…»), поэтому запрос идёт через отдельный сетевой менеджер —
см. OWN_NETWORK и GeoSearchRU._manager. Прокси из настроек QGIS туда переносится.
"""

import json
import math
import re

from qgis.PyQt.QtCore import QSettings, QUrl, QUrlQuery
from qgis.PyQt.QtNetwork import QNetworkRequest

from .providers import Result, _json_message

# Кадастровый номер: квартал (три части) или объект (четыре).
# Пример: 68:29:0309001 и 68:29:0309001:24. Нули в номере квартала бывают (50:12:0000000:123).
CADASTRAL_NUMBER = re.compile(r"^\d{1,2}:\d{1,2}:\d{1,7}(:\d{1,12})?$")

EARTH_RADIUS = 6378137.0  # сфера EPSG:3857


def is_cadastral_number(text):
    """Пользователь ввёл кадастровый номер, а не адрес."""
    return bool(CADASTRAL_NUMBER.match(normalize_number(text)))


def normalize_number(text):
    """«68 : 29 : 0309001 : 24» → «68:29:0309001:24»."""
    return re.sub(r"\s+", "", str(text or "")).strip(":")


def _to_wgs84(x, y):
    """EPSG:3857 → WGS 84. Формула точная: 3857 определён на сфере."""
    longitude = x / EARTH_RADIUS * 180.0 / math.pi
    latitude = (2.0 * math.atan(math.exp(y / EARTH_RADIUS)) - math.pi / 2.0) * 180.0 / math.pi
    return longitude, latitude


def _ring(points, box):
    """Кольцо в WKT; box копит вершины для рамки объекта — без замыкающего повтора."""
    parts = []
    for point in points:
        longitude, latitude = _to_wgs84(float(point[0]), float(point[1]))
        box.append((longitude, latitude))
        parts.append(f"{longitude:.8f} {latitude:.8f}")
    if parts and parts[0] != parts[-1]:
        parts.append(parts[0])  # WKT требует замкнутое кольцо
    return "(" + ", ".join(parts) + ")"


def _polygon(rings, box):
    return "(" + ", ".join(_ring(ring, box) for ring in rings) + ")"


def geometry_wkt(geometry):
    """GeoJSON из НСПД → (WKT в WGS 84, [(долгота, широта), …] для рамки).

    Возвращает (None, []) для объекта без графики — в ЕГРН такие есть.
    """
    if not isinstance(geometry, dict):
        return None, []
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    box = []
    if not coordinates:
        return None, []
    if kind == "Point":
        longitude, latitude = _to_wgs84(float(coordinates[0]), float(coordinates[1]))
        box.append((longitude, latitude))
        return f"POINT({longitude:.8f} {latitude:.8f})", box
    if kind == "MultiPoint":
        points = [_to_wgs84(float(p[0]), float(p[1])) for p in coordinates]
        box.extend(points)
        return "MULTIPOINT(" + ", ".join(f"({x:.8f} {y:.8f})" for x, y in points) + ")", box
    if kind == "LineString":
        return "LINESTRING" + _ring(coordinates, box), box
    if kind == "MultiLineString":
        return "MULTILINESTRING(" + ", ".join(_ring(line, box) for line in coordinates) + ")", box
    if kind == "Polygon":
        return "POLYGON" + _polygon(coordinates, box), box
    if kind == "MultiPolygon":
        return "MULTIPOLYGON(" + ", ".join(_polygon(rings, box) for rings in coordinates) + ")", box
    return None, []


def _scale(box):
    """Масштаб карты по размеру объекта: чтобы он занял примерно треть экрана."""
    if not box:
        return 10000
    longitudes = [point[0] for point in box]
    latitudes = [point[1] for point in box]
    middle = math.radians(sum(latitudes) / len(latitudes))
    width = (max(longitudes) - min(longitudes)) * 111320 * max(math.cos(middle), 0.1)
    height = (max(latitudes) - min(latitudes)) * 110540
    size = max(width, height, 10.0)
    return int(min(max(round(size * 15, -2), 500), 500000))


# Поля временного слоя и карточки: ключ НСПД → (имя поля, подпись).
# У разных категорий ЕГРН свои имена, поэтому у одного поля бывает несколько источников.
FIELD_SOURCES = (
    ("cad_num", "Кадастровый номер", ("cad_num", "cad_number")),
    ("address", "Адрес", ("readable_address", "address", "addr")),
    ("obj_type", "Вид объекта", ("land_record_type", "build_record_type", "params_type", "type")),
    ("purpose", "Назначение", ("purpose", "build_record_purpose", "obj_name", "name")),
    ("area", "Площадь, м²", ("area", "build_record_area", "declared_area", "area_value")),
    ("land_cat", "Категория земель", ("land_record_category_type",)),
    ("use", "Разрешённое использование",
     ("permitted_use_established_by_document", "permitted_use_name", "permitted_use")),
    ("floors", "Этажность", ("floors", "build_record_floors")),
    ("year_built", "Год постройки", ("year_built", "build_record_year_built")),
    ("cost", "Кадастровая стоимость, ₽", ("cost_value",)),
    ("cost_date", "Дата определения стоимости", ("cost_determination_date", "cost_application_date")),
    ("reg_date", "Дата постановки на учёт",
     ("land_record_reg_date", "build_record_registration_date", "registration_date")),
    ("quarter", "Кадастровый квартал", ("quarter_cad_number",)),
    ("status", "Статус", ("status", "land_record_subtype", "build_record_subtype")),
)
FIELD_NAMES = tuple(name for name, _label, _keys in FIELD_SOURCES)
FIELD_LABELS = {name: label for name, label, _keys in FIELD_SOURCES}
# Числовые поля временного слоя: остальные хранятся строками, как пришли.
NUMERIC_FIELDS = {"area", "cost", "floors"}


def _clean(value):
    """Значение из ответа в вид, годный для поля и для карточки."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (_clean(item) for item in value) if part)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())


class Nspd:
    """Геопортал НСПД (nspd.gov.ru) — публичная кадастровая карта Росреестра.

    Ключа не требует. В списке источников поиска адреса не показывается:
    включается сам, когда в строке поиска кадастровый номер.
    """

    ID = "nspd"
    TITLE = "НСПД (кадастр)"
    SOURCE = "НСПД (ЕГРН)"
    NEEDS_TOKEN = False
    NEEDS_SECRET = False
    SEARCHABLE = False  # не источник поиска адреса, в списке его нет
    COARSE_WARNING = False
    SUPPORTS_BBOX = False
    SETTINGS_TOKEN = SETTINGS_SECRET = None
    OWN_NETWORK = True  # нужен свой User-Agent, см. заголовок модуля
    ATTRIBUTION = (
        'Сведения ЕГРН — <a href="https://nspd.gov.ru/map">НСПД, Росреестр</a>. '
        'Данные публичной кадастровой карты носят справочный характер и не заменяют выписку из ЕГРН: '
        'границы и площадь могут быть уточнены, а участки без координат на карте не показываются.'
    )
    MIN_INTERVAL_MS = 1500  # НСПД перестаёт отвечать, если частить
    TIMEOUT_MS = 15000  # с тремя попытками ожидание и так до минуты, см. MAX_ATTEMPTS
    DEFAULT_URL = "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
    SETTINGS_URL = "GeoSearchRU/nspd_url"
    THEMATIC_SEARCH_ID = 1  # поиск по ЕГРН
    # Защита НСПД смотрит на заголовки браузера, поэтому запрос представляется браузером.
    USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
    # Referer с параметром: так выглядит переход с карты НСПД, и только такой набор
    # проходил защиту вживую (голый /map давал 597). Тот же адрес в рабочих примерах
    # из обсуждений НСПД.
    REFERER = "https://nspd.gov.ru/map?thematic=PKK"
    # 597 и 429 у НСПД — временные: защита отбивает всплеск, но следом пропускает.
    # Столько же попыток и такие же паузы у работающих клиентов НСПД.
    RETRY_STATUSES = (429, 500, 502, 503, 504, 597)
    MAX_ATTEMPTS = 3
    RETRY_DELAY_MS = 3000
    HTTP_HINTS = {
        400: "некорректный кадастровый номер",
        403: "НСПД отклонил запрос (защита от автоматических обращений)",
        404: "объект с таким номером не найден",
        429: "слишком много запросов, повторите позже",
        500: "внутренняя ошибка НСПД",
        597: "НСПД отклонил запрос (защита от автоматических обращений)",
    }
    TIMEOUT_HINT = ("Он доступен только из России: проверьте подключение "
                    "и отключите VPN, если он включён.")

    def url(self):
        settings = QSettings()
        if not settings.contains(self.SETTINGS_URL):
            # Пишется в настройки, чтобы адрес было видно в «Параметры → Дополнительно».
            settings.setValue(self.SETTINGS_URL, self.DEFAULT_URL)
        return settings.value(self.SETTINGS_URL, "", type=str).strip() or self.DEFAULT_URL

    def request(self, query, token="", count=10, secret="", signature_mode=None, bbox=None):
        number = normalize_number(query)
        # Через QUrlQuery, а не склейкой: свой адрес сервера может уже содержать «?».
        url = QUrl(self.url())
        parameters = QUrlQuery(url.query())
        parameters.addQueryItem("query", number)
        parameters.addQueryItem("thematicSearchId", str(self.THEMATIC_SEARCH_ID))
        url.setQuery(parameters)
        request = QNetworkRequest(url)
        for name, value in (
            # User-Agent задаётся здесь: запрос идёт мимо QgsNetworkAccessManager (OWN_NETWORK),
            # а Qt по умолчанию присылает короткий «Mozilla/5.0», которого защите НСПД мало.
            (b"User-Agent", self.USER_AGENT.encode()),
            (b"Accept", b"application/json, text/plain, */*"),
            (b"Accept-Language", b"ru-RU,ru,en-US,en,uk"),
            (b"Referer", self.REFERER.encode()),
            # Origin и запрет кэша есть у работающих клиентов НСПД; без них защита придирчивее.
            (b"Origin", b"https://nspd.gov.ru"),
            (b"Cache-Control", b"no-cache"),
            (b"Pragma", b"no-cache"),
            (b"Sec-Fetch-Dest", b"empty"),
            (b"Sec-Fetch-Mode", b"cors"),
            (b"Sec-Fetch-Site", b"same-origin"),
            # Без этих трёх защита НСПД отвечает 597 Forbidden: она сверяет полный набор
            # заголовков браузера, а не один User-Agent. Версия здесь и в USER_AGENT — одна.
            (b"sec-ch-ua", b'"Chromium";v="140", "Google Chrome";v="140", "Not:A-Brand";v="24"'),
            (b"sec-ch-ua-mobile", b"?0"),
            (b"sec-ch-ua-platform", b'"macOS"'),
        ):
            request.setRawHeader(name, value)
        # Запрос идёт по HTTP/1.1: все успешные ответы НСПД получены только так,
        # а с предложенным HTTP/2 соединение открывалось и зависало (то же было с curl).
        # В Qt5 атрибут называется HTTP2AllowedAttribute, в Qt6 — Http2AllowedAttribute.
        http2 = getattr(QNetworkRequest.Attribute, "Http2AllowedAttribute",
                        getattr(QNetworkRequest.Attribute, "HTTP2AllowedAttribute", None))
        if http2 is not None:
            request.setAttribute(http2, False)
        return request, None

    def reverse(self, latitude, longitude, token="", count=10, secret="", signature_mode=None):
        raise NotImplementedError("НСПД ищет по кадастровому номеру, а не по точке")

    def parse(self, body):
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("response is not an object")
        features = ((data.get("data") or {}).get("features") or []) if isinstance(data.get("data"), dict) else []
        return [self._parse_one(f) for f in features if isinstance(f, dict)]

    def _parse_one(self, feature):
        properties = feature.get("properties") or {}
        options = properties.get("options") or {}
        values = {}
        for name, _label, keys in FIELD_SOURCES:
            for key in keys:
                cleaned = _clean(options.get(key))
                if cleaned:
                    values[name] = cleaned
                    break
        number = values.get("cad_num") or _clean(properties.get("label") or properties.get("descr"))
        if number:
            values["cad_num"] = number
        kind = _clean(properties.get("categoryName")) or "Объект ЕГРН"
        address = values.get("address")
        title = f"{number} — {kind}" if number else kind
        if address:
            title = f"{title}, {address}"
        wkt, box = geometry_wkt(feature.get("geometry"))
        details = "\n".join(
            f"{FIELD_LABELS[name]}: {values[name]}"
            for name in FIELD_NAMES if values.get(name) and name not in ("cad_num", "address"))
        if wkt is None:
            # Объект есть в ЕГРН, но границы не внесены — показываем сведения без карты.
            return Result(title, None, None, "без границ в ЕГРН", True, None, {}, details, None)
        longitude = sum(point[0] for point in box) / len(box)
        latitude = sum(point[1] for point in box) / len(box)
        precision = "точка" if wkt.startswith("POINT") else "границы по ЕГРН"
        return Result(title, longitude=longitude, latitude=latitude, precision=precision,
                      coarse=False, scale=_scale(box), parts={}, details=details,
                      geometry=(wkt, dict(values), kind))

    error_detail = staticmethod(_json_message)


__all__ = ["Nspd", "is_cadastral_number", "normalize_number", "geometry_wkt",
           "FIELD_NAMES", "FIELD_LABELS", "NUMERIC_FIELDS"]
