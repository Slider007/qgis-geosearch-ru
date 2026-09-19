"""Address search services: how to ask each one and how to read its answer.

Every provider turns a response into a list of Result objects, so the dialog,
the marker and the points layer do not depend on the service.
"""

import base64
import hashlib
import hmac
import json
from collections import namedtuple
from datetime import datetime, timezone
from urllib.parse import quote

from qgis.PyQt.QtCore import QSettings, QUrl, QUrlQuery
from qgis.PyQt.QtNetwork import QNetworkRequest

PLUGIN_URL = "https://github.com/Slider007/qgis-geosearch-ru"

# precision: text for the user; coarse: coordinates are not at house level; scale: map scale to zoom to.
Result = namedtuple("Result", "address latitude longitude precision coarse scale")

NO_COORDINATES = "без координат"


def _result(address, latitude, longitude, precision, coarse, scale):
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return Result(address, None, None, NO_COORDINATES, True, None)
    return Result(address, latitude, longitude, precision, coarse, scale)


def _json_message(body):
    """Error text a service put into its JSON error body, if any."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    message = data.get("message") or (error.get("message") if isinstance(error, dict) else error)
    return message if isinstance(message, str) else None


class Dadata:
    ID = "dadata"
    TITLE = "DaData (нужен токен)"
    SOURCE = "DaData"
    NEEDS_TOKEN = True
    NEEDS_SECRET = False
    TOKEN_LABEL = "Токен DaData"
    TOKEN_URL = "https://dadata.ru/profile/#info"
    TOKEN_HINT = "Открыть личный кабинет DaData: после регистрации там будет API-ключ"
    TOKEN_PLACEHOLDER = "Токен DaData API"
    SETTINGS_TOKEN = "GeoSearchRU/dadata_token"
    SETTINGS_SECRET = None
    ATTRIBUTION = None
    MIN_INTERVAL_MS = 0
    URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"
    HTTP_HINTS = {
        400: "некорректный запрос",
        401: "не указан API-токен",
        403: "неверный токен, не подтверждена почта или исчерпан дневной лимит",
        413: "слишком длинный запрос",
        429: "слишком много запросов, повторите позже",
    }
    # data.qc_geo → (precision, map scale); 3 and above are coarse.
    QC_GEO = {
        0: ("точные координаты дома", 2500),
        1: ("ближайший дом", 2500),
        2: ("улица", 10000),
        3: ("населённый пункт", 50000),
        4: ("город", 100000),
    }

    def request(self, query, token, count, secret=""):
        request = QNetworkRequest(QUrl(self.URL))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setRawHeader(b"Accept", b"application/json")
        request.setRawHeader(b"Authorization", f"Token {token}".encode())
        return request, json.dumps({"query": query, "count": count}).encode()

    def parse(self, body):
        suggestions = json.loads(body)["suggestions"]
        if not isinstance(suggestions, list):
            raise ValueError("suggestions is not a list")
        return [self._parse_one(s) for s in suggestions if isinstance(s, dict)]

    def _parse_one(self, suggestion):
        data = suggestion.get("data") or {}
        try:
            qc_geo = int(data.get("qc_geo"))
        except (TypeError, ValueError):
            qc_geo = None
        precision, scale = self.QC_GEO.get(qc_geo, ("точность неизвестна", 50000))
        address = suggestion.get("unrestricted_value") or suggestion.get("value") or ""
        return _result(address, data.get("geo_lat"), data.get("geo_lon"), precision, qc_geo is None or qc_geo >= 3, scale)

    error_detail = staticmethod(_json_message)


class Nominatim:
    """Public OpenStreetMap geocoder.

    Usage policy (https://operations.osmfoundation.org/policies/nominatim/):
    at most 1 request per second, no autocomplete and no bulk geocoding, an
    identifying Referer, visible attribution, repeated queries cached,
    and the server must be switchable without a plugin update — hence the URL
    is read from settings (GeoSearchRU/nominatim_url).
    """

    ID = "nominatim"
    TITLE = "OpenStreetMap (Nominatim), без токена"
    SOURCE = "OpenStreetMap"
    NEEDS_TOKEN = False
    NEEDS_SECRET = False
    SETTINGS_TOKEN = SETTINGS_SECRET = None
    ATTRIBUTION = (
        'Данные © <a href="https://www.openstreetmap.org/copyright">участники OpenStreetMap</a>, лицензия ODbL. '
        'Публичный сервер Nominatim: только поиск по кнопке, не чаще раза в секунду, без массового поиска — '
        '<a href="https://operations.osmfoundation.org/policies/nominatim/">правила использования</a>.'
    )
    MIN_INTERVAL_MS = 1000
    DEFAULT_URL = "https://nominatim.openstreetmap.org/search"
    SETTINGS_URL = "GeoSearchRU/nominatim_url"
    HTTP_HINTS = {
        400: "некорректный запрос",
        403: "сервер Nominatim отказал в доступе — возможно, превышены правила использования",
        429: "слишком много запросов: не чаще раза в секунду, повторите позже",
    }

    def url(self):
        settings = QSettings()
        if not settings.contains(self.SETTINGS_URL):
            # Written out so the key shows up in QGIS Options → Advanced, where it can be changed.
            settings.setValue(self.SETTINGS_URL, self.DEFAULT_URL)
        return settings.value(self.SETTINGS_URL, "", type=str).strip() or self.DEFAULT_URL

    def request(self, query, token, count, secret=""):
        url = QUrl(self.url())
        params = QUrlQuery()
        for key, value in (
            ("q", query), ("format", "jsonv2"), ("addressdetails", "1"), ("limit", str(count)),
            ("countrycodes", "ru"), ("accept-language", "ru"),
        ):
            params.addQueryItem(key, value)
        url.setQuery(params)
        request = QNetworkRequest(url)
        request.setRawHeader(b"Accept", b"application/json")
        # The policy asks every application to identify itself by User-Agent or Referer.
        # QgsNetworkAccessManager replaces User-Agent with its own "QGIS/…", so Referer it is.
        request.setRawHeader(b"Referer", PLUGIN_URL.encode())
        return request, None

    def parse(self, body):
        places = json.loads(body)
        if not isinstance(places, list):
            raise ValueError("response is not a list")
        return [self._parse_one(p) for p in places if isinstance(p, dict)]

    @staticmethod
    def _parse_one(place):
        try:
            rank = int(place.get("place_rank"))
        except (TypeError, ValueError):
            rank = 0
        has_house = bool((place.get("address") or {}).get("house_number"))
        # place_rank: 30 — building, 26–27 — street, 17–25 — village or district, 16 and below — city or larger.
        if has_house or rank >= 28:
            precision, coarse, scale = "дом", False, 2500
        elif rank >= 26:
            precision, coarse, scale = "улица", False, 10000
        elif rank >= 17:
            precision, coarse, scale = "населённый пункт или район", True, 50000
        else:
            precision, coarse, scale = "город или крупнее", True, 100000
        return _result(place.get("display_name") or "", place.get("lat"), place.get("lon"), precision, coarse, scale)

    error_detail = staticmethod(_json_message)


class Yandex:
    """Yandex Geocoder API v1 (https://yandex.ru/maps-api/docs/geocoder-api/).

    Storing results (the points layer) needs the extended commercial licence.
    With a signing secret every request is signed as described in
    https://yandex.ru/maps-api/docs/common/security/signature_usage.html
    """

    ID = "yandex"
    TITLE = "Яндекс (нужен ключ)"
    SOURCE = "Яндекс"
    NEEDS_TOKEN = True
    NEEDS_SECRET = True
    TOKEN_LABEL = "Ключ Яндекса"
    TOKEN_URL = "https://developer.tech.yandex.ru/"
    TOKEN_HINT = "Открыть Кабинет разработчика Яндекса: ключ пакета «API Геокодера» и секрет подписи"
    TOKEN_PLACEHOLDER = "API-ключ Геокодера"
    SETTINGS_TOKEN = "GeoSearchRU/yandex_key"
    SETTINGS_SECRET = "GeoSearchRU/yandex_secret"
    SETTINGS_SIGNATURE = "GeoSearchRU/yandex_signature"  # "ttl" (default) or "plain"
    ATTRIBUTION = 'Данные © <a href="https://yandex.ru/legal/maps_api/">Яндекс</a>.'
    MIN_INTERVAL_MS = 0
    URL = "https://geocode-maps.yandex.ru/v1/"
    HTTP_HINTS = {
        400: "некорректный запрос",
        403: "неверный ключ или подпись, либо ключ ещё не активирован (до 15 минут после получения)",
        429: "слишком много запросов или исчерпан суточный лимит",
    }
    # GeocoderMetaData.precision for houses → (precision, coarse, scale).
    HOUSE_PRECISION = {
        "exact": ("точные координаты дома", False, 2500),
        "number": ("дом, другой корпус или строение", False, 2500),
        "near": ("ближайший дом", False, 2500),
        "range": ("приблизительные координаты дома", False, 2500),
        "street": ("улица", False, 10000),
    }
    # GeocoderMetaData.kind when no house or street matched.
    KIND_PRECISION = {
        "district": ("район города", True, 25000),
        "locality": ("населённый пункт", True, 50000),
        "area": ("район области", True, 250000),
        "province": ("регион", True, 1000000),
        "country": ("страна", True, 10000000),
    }

    def request(self, query, token, count, secret=""):
        params = [("geocode", query), ("lang", "ru_RU"), ("format", "json"), ("results", str(count)), ("apikey", token)]
        path = "/v1/?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
        if secret:
            path += "&signature=" + self.signature(path, secret, QSettings().value(self.SETTINGS_SIGNATURE, "ttl", type=str))
        # fromEncoded keeps the query byte for byte as signed.
        request = QNetworkRequest(QUrl.fromEncoded(("https://geocode-maps.yandex.ru" + path).encode()))
        request.setRawHeader(b"Accept", b"application/json")
        return request, None

    @staticmethod
    def signature(path, secret, mode="ttl", now=None):
        key = base64.urlsafe_b64decode(secret.strip() + "=" * (-len(secret.strip()) % 4))
        if mode != "plain":
            hour = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:00:00Z")
            key = hmac.new(key, hour.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(hmac.new(key, path.encode(), hashlib.sha256).digest()).decode()

    def parse(self, body):
        members = json.loads(body)["response"]["GeoObjectCollection"]["featureMember"]
        if not isinstance(members, list):
            raise ValueError("featureMember is not a list")
        return [self._parse_one(m["GeoObject"]) for m in members if isinstance(m, dict) and "GeoObject" in m]

    def _parse_one(self, geo):
        meta = (geo.get("metaDataProperty") or {}).get("GeocoderMetaData") or {}
        kind, precision = meta.get("kind"), meta.get("precision")
        if precision in self.HOUSE_PRECISION:
            label, coarse, scale = self.HOUSE_PRECISION[precision]
        else:
            label, coarse, scale = self.KIND_PRECISION.get(kind, ("объект, не адрес", True, 10000))
        address = meta.get("text") or geo.get("name") or ""
        postal = (meta.get("Address") or {}).get("postal_code")
        if postal:
            address = f"{postal}, {address}"
        try:
            longitude, latitude = (geo.get("Point") or {}).get("pos", "").split()
        except ValueError:
            longitude = latitude = None
        return _result(address, latitude, longitude, label, coarse, scale)

    error_detail = staticmethod(_json_message)
