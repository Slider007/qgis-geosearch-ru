"""«Что здесь?» — объекты OpenStreetMap в точке карты через Overpass API.

Это не геокодер: он не ищет адрес, а перечисляет, что в этой точке нарисовано в
OSM — здание с этажностью, участок, дорогу с покрытием, ЛЭП с напряжением — и
границы, внутри которых точка лежит (город, район, субъект, страна).

Правила общественного сервера (https://dev.overpass-api.de/overpass-doc/en/preface/commons.html):
до 10 000 запросов в сутки и до 1 ГБ трафика, запросы становятся в очередь,
поэтому запрос идёт только по щелчку, с паузой и с кэшем повторов.
"""

import json
from urllib.parse import quote

from qgis.PyQt.QtCore import QSettings, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .providers import PLUGIN_URL, _parts, _result

# Ключ тега → (название по-русски, {значение: расшифровка}). Порядок важен:
# берётся первый подошедший ключ, поэтому сначала идут самые говорящие.
CATEGORIES = [
    ("building", "Здание", {
        "yes": "", "house": "жилой дом", "residential": "жилое", "apartments": "многоквартирный дом",
        "detached": "частный дом", "garage": "гараж", "garages": "гаражи", "industrial": "производственное",
        "warehouse": "склад", "commercial": "коммерческое", "retail": "торговое", "office": "офисное",
        "school": "школа", "kindergarten": "детский сад", "hospital": "больница", "church": "храм",
        "service": "техническое", "shed": "сарай", "roof": "навес", "construction": "строится",
        "transformer_tower": "трансформаторная будка", "farm": "сельхозпостройка",
    }),
    ("power", "Электросети", {
        "line": "ЛЭП", "minor_line": "линия низкого напряжения", "tower": "опора ЛЭП", "pole": "опора",
        "portal": "портал", "substation": "подстанция", "transformer": "трансформатор",
        "generator": "генератор", "plant": "электростанция", "switchgear": "распредустройство",
        "cable": "кабельная линия", "catenary_mast": "опора контактной сети",
    }),
    ("highway", "Дорога", {
        "motorway": "автомагистраль", "trunk": "скоростная", "primary": "основная",
        "secondary": "второстепенная", "tertiary": "местного значения", "unclassified": "без категории",
        "residential": "улица", "living_street": "жилая зона", "service": "проезд", "track": "грунтовая",
        "path": "тропа", "footway": "тротуар", "cycleway": "велодорожка", "steps": "лестница",
        "bus_stop": "остановка", "crossing": "переход", "construction": "строится",
    }),
    ("railway", "Железная дорога", {
        "rail": "путь", "station": "станция", "halt": "остановочный пункт", "level_crossing": "переезд",
        "tram": "трамвайные пути", "subway": "метро", "abandoned": "заброшенный путь",
    }),
    ("waterway", "Водоток", {"river": "река", "stream": "ручей", "ditch": "канава", "drain": "водосток",
                             "canal": "канал", "dam": "плотина"}),
    ("natural", "Природный объект", {"water": "водоём", "wood": "лес", "scrub": "кустарник",
                                     "wetland": "болото", "grassland": "луг", "tree": "дерево",
                                     "tree_row": "ряд деревьев", "sand": "песок", "cliff": "обрыв"}),
    ("landuse", "Землепользование", {
        "residential": "жилая застройка", "industrial": "промышленная зона", "commercial": "коммерческая зона",
        "retail": "торговая зона", "farmland": "пашня", "meadow": "луг", "forest": "лес",
        "orchard": "сад", "allotments": "садовые участки", "cemetery": "кладбище", "garages": "гаражи",
        "construction": "стройка", "quarry": "карьер", "grass": "газон", "village_green": "озеленение",
    }),
    ("amenity", "Заведение", {
        "school": "школа", "kindergarten": "детский сад", "hospital": "больница", "clinic": "поликлиника",
        "pharmacy": "аптека", "bank": "банк", "cafe": "кафе", "restaurant": "ресторан", "fuel": "АЗС",
        "parking": "парковка", "police": "полиция", "post_office": "почта", "school_yard": "школьный двор",
        "townhall": "администрация", "place_of_worship": "храм", "fire_station": "пожарная часть",
    }),
    ("shop", "Магазин", {"supermarket": "супермаркет", "convenience": "продукты", "bakery": "пекарня",
                         "clothes": "одежда", "hardware": "хозтовары", "car_repair": "автосервис",
                         "travel_agency": "турагентство"}),
    ("office", "Офис", {"company": "компания", "government": "госучреждение", "estate_agent": "агентство"}),
    ("leisure", "Отдых", {"park": "парк", "garden": "сквер", "pitch": "спортплощадка",
                          "playground": "детская площадка", "sports_centre": "спорткомплекс",
                          "stadium": "стадион"}),
    ("man_made", "Сооружение", {"tower": "башня", "mast": "мачта", "water_tower": "водонапорная башня",
                                "pipeline": "трубопровод", "storage_tank": "резервуар",
                                "surveillance": "камера", "survey_point": "геодезический пункт"}),
    ("barrier", "Ограждение", {"fence": "забор", "wall": "стена", "gate": "ворота", "bollard": "столбик"}),
    ("tourism", "Туризм", {"hotel": "гостиница", "museum": "музей", "attraction": "достопримечательность",
                           "viewpoint": "смотровая площадка"}),
    ("historic", "Памятник", {"monument": "монумент", "memorial": "мемориал", "ruins": "руины"}),
    ("boundary", "Граница", {"administrative": "административная", "protected_area": "охраняемая территория",
                             "national_park": "национальный парк", "economic": "экономический район"}),
    ("place", "Место", {"city": "город", "town": "город", "village": "село", "hamlet": "деревня",
                        "suburb": "район города", "neighbourhood": "микрорайон", "region": "регион",
                        "county": "район", "state": "субъект", "country": "страна",
                        "isolated_dwelling": "хутор", "locality": "местность"}),
]

# Теги, которые стоит показать в карточке объекта.
DETAIL_TAGS = {
    "building:levels": "Этажей", "height": "Высота, м", "start_date": "Построено",
    "surface": "Покрытие", "lanes": "Полос", "maxspeed": "Ограничение скорости", "ref": "Номер",
    "voltage": "Напряжение, В", "cables": "Проводов", "circuits": "Цепей", "wires": "Проводов в фазе",
    "line": "Тип линии", "material": "Материал", "operator": "Обслуживает", "owner": "Владелец",
    "opening_hours": "Часы работы", "phone": "Телефон", "contact:phone": "Телефон", "website": "Сайт",
    "admin_level": "Уровень границы", "population": "Население", "official_status": "Статус",
    "description": "Описание", "note": "Примечание", "layer": "Слой", "location": "Расположение",
    "protect_class": "Класс охраны", "landuse": "Землепользование", "amenity": "Назначение",
}

# admin_level границы → (подпись, масштаб карты). Чем меньше уровень, тем крупнее территория.
ADMIN_LEVELS = {
    "2": ("страна", 10000000), "3": ("федеральный округ", 5000000), "4": ("субъект", 1000000),
    "5": ("часть субъекта", 500000), "6": ("район", 250000), "7": ("поселение", 100000),
    "8": ("город или поселение", 50000), "9": ("район города", 25000), "10": ("микрорайон", 10000),
    "11": ("квартал", 5000),
}


def _name(tags):
    return tags.get("name") or tags.get("name:ru") or tags.get("official_name") or tags.get("int_name") or ""


def _address(tags):
    """Адрес из тегов addr:* — той же формы, что у геокодеров."""
    return _parts(postcode=tags.get("addr:postcode"), region=tags.get("addr:region"),
                  area=tags.get("addr:district"), city=tags.get("addr:city"),
                  street=tags.get("addr:street"), house=tags.get("addr:housenumber"))


# Площади из is_in, которые стоит показывать: границы территорий, а не экономические районы и прочее.
AREA_BOUNDARIES = ("administrative", "protected_area", "national_park")


def _category_index(tags):
    """Порядок вида объекта в списке: сначала здания, потом сети, дороги и остальное."""
    for index, (key, _, _) in enumerate(CATEGORIES):
        if tags.get(key):
            return index
    return len(CATEGORIES)


def _category(tags):
    """(тег, название вида, уточнение) по самому говорящему тегу объекта."""
    for key, title, values in CATEGORIES:
        value = tags.get(key)
        if value:
            return key, title, values.get(value, value.replace("_", " "))
    for key, value in tags.items():
        if ":" not in key and key not in ("name", "type", "source", "wikidata", "wikipedia"):
            return key, key, value.replace("_", " ")
    return "", "Объект OSM", ""


class Overpass:
    """Источник «Что здесь?»: не ищет адрес, а рассказывает, что в точке есть."""

    ID = "overpass"
    TITLE = SOURCE = "OpenStreetMap"
    SEARCHABLE = False  # в списке источников поиска адреса не показывается
    NEEDS_TOKEN = NEEDS_SECRET = False
    SETTINGS_TOKEN = SETTINGS_SECRET = None
    SUPPORTS_BBOX = False
    COARSE_WARNING = False  # «приблизительные координаты» для объекта не предупреждение, а суть
    ATTRIBUTION = (
        'Данные © <a href="https://www.openstreetmap.org/copyright">участники OpenStreetMap</a>, лицензия ODbL. '
        'Сведения об объектах — из OSM: они могут быть неполными или устаревшими.'
    )
    MIN_INTERVAL_MS = 2000
    TIMEOUT_MS = 30000
    DEFAULT_URL = "https://overpass-api.de/api/interpreter"
    SETTINGS_URL = "GeoSearchRU/overpass_url"
    RADIUS_M = 30
    MAX_ELEMENTS = 60
    HTTP_HINTS = {
        400: "сервер не понял запрос",
        429: "сервер Overpass занят: слишком много запросов, повторите через минуту",
        504: "сервер Overpass не успел ответить, повторите позже",
    }

    def url(self):
        settings = QSettings()
        if not settings.contains(self.SETTINGS_URL):
            # Пишется в настройки, чтобы адрес можно было сменить в «Параметры → Дополнительно».
            settings.setValue(self.SETTINGS_URL, self.DEFAULT_URL)
        return settings.value(self.SETTINGS_URL, "", type=str).strip() or self.DEFAULT_URL

    def query(self, latitude, longitude):
        # Только точки и линии вокруг: перебор отношений (nwr) сервер отбивает по таймауту,
        # а территории, внутри которых лежит точка, и так приходят из is_in.
        point = f"{latitude:.6f},{longitude:.6f}"
        around = f"(around:{self.RADIUS_M},{point})"
        return (f"[out:json][timeout:25];is_in({point})->.a;"
                f"(.a;way{around};node{around};);out tags center {self.MAX_ELEMENTS};")

    def reverse(self, latitude, longitude, token="", count=0, secret="", signature_mode=None):
        request = QNetworkRequest(QUrl(self.url()))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/x-www-form-urlencoded")
        request.setRawHeader(b"Accept", b"application/json")
        # QgsNetworkAccessManager подменяет User-Agent своим, поэтому модуль называет себя через Referer.
        request.setRawHeader(b"Referer", PLUGIN_URL.encode())
        body = "data=" + quote(self.query(latitude, longitude), safe="")
        self.point = (latitude, longitude)
        return request, body.encode()

    def parse(self, body):
        elements = json.loads(body).get("elements")
        if not isinstance(elements, list):
            raise ValueError("elements is not a list")
        latitude, longitude = getattr(self, "point", (None, None))
        rows = []
        for element in elements:
            if not (isinstance(element, dict) and element.get("tags")):
                continue  # вершины линий без тегов пользователю не нужны
            row = self._parse_one(element, latitude, longitude)
            if row:
                rows.append(row)
        # Сначала сами объекты, затем территории от меньшей к большей; внутри — по виду объекта.
        rows.sort(key=lambda row: row[0])
        return [result for _, result in rows]

    def _parse_one(self, element, click_latitude, click_longitude):
        tags = element["tags"]
        key, title, detail = _category(tags)
        area = element.get("type") == "area"
        if area and not (tags.get("boundary") in AREA_BOUNDARIES or tags.get("place") or tags.get("landuse")):
            return None  # прочие площади из is_in только зашумляют список
        admin = ADMIN_LEVELS.get(tags.get("admin_level", "")) if area else None
        if admin:
            detail, scale = admin[0], admin[1]
        else:
            scale = 100000 if area else 2500
        name = _name(tags)
        address = ", ".join(p for p in (
            f"{title}: {detail}" if detail else title,
            name and f"«{name}»",
            ", ".join(v for k, v in _address(tags).items() if k in ("street", "house")),
        ) if p)
        centre = element.get("center") or element
        # У площадей из is_in геометрии в ответе нет: точка пользователя лежит внутри них.
        latitude = centre.get("lat", click_latitude if area else None)
        longitude = centre.get("lon", click_longitude if area else None)
        result = _result(address, latitude, longitude, detail or title.lower(), area, scale,
                         _address(tags), self.details(tags, key))
        return (scale, _category_index(tags)), result

    @staticmethod
    def details(tags, category_key=""):
        """Теги объекта человеческим языком — для карточки под списком."""
        lines = []
        for key, label in DETAIL_TAGS.items():
            if tags.get(key):
                lines.append(f"{label}: {tags[key]}")
        # Тег, который уже назвал вид объекта, и служебные теги во второй раз не показываем.
        skip = set(DETAIL_TAGS) | {"name", "admin_level", "type", "boundary", "place", category_key}
        rest = [f"{k}={v}" for k, v in sorted(tags.items()) if k not in skip and ":" not in k]
        if rest:
            lines.append("Теги OSM: " + ", ".join(rest[:12]))
        return "\n".join(lines)

    @staticmethod
    def error_detail(body):
        try:
            remark = json.loads(body).get("remark")
        except ValueError:
            return None
        return remark if isinstance(remark, str) else None
