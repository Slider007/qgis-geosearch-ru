"""Окна модуля для снимков: scripts/ui_snap.py из навыка qgis-ui-review.

Запуск:
    SK=~/.claude/skills
    $SK/qgis-plugin/scripts/qgis_env.sh -- python $SK/qgis-ui-review/scripts/ui_snap.py tests/ui_shots.py

Окна снимаются в тех состояниях, в которых их видит человек: пустое, с найденным
адресом, с найденным объектом ЕГРН, с ошибкой, во время запроса.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN_ROOT)

from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402


class Iface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.canvas.setExtent(QgsRectangle(4600000, 6900000, 4650000, 6950000))

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return None


SOURCES = [("dadata", "DaData"), ("yandex", "Яндекс Геокодер"), ("nominatim", "OpenStreetMap (без ключа)")]

ADDRESS_CARD = """392000, Тамбовская обл, г Тамбов, пр-кт Энергетиков, д 7

Координаты WGS 84:
Широта: 52.720100
Долгота: 41.450200
Точность: дом
Источник: DaData

Разбор адреса:
Индекс: 392000
Регион: Тамбовская обл
Город или населённый пункт: г Тамбов
Улица: пр-кт Энергетиков
Дом: 7"""

CADASTRE_CARD = """28:10:000000:7270 — Земельные участки ЕГРН, Российская Федерация, Амурская область

Координаты WGS 84:
Широта: 53.152400
Долгота: 127.531900
Точность: границы по ЕГРН
Источник: НСПД (ЕГРН)

Сведения:
Вид объекта: Земельный участок
Категория земель: Земли промышленности, энергетики, транспорта, связи, радиовещания, телевидения, информатики, земли для обеспечения космической деятельности, земли обороны, безопасности и земли иного специального назначения
Разрешённое использование: Для промышленно-производственных нужд
Кадастровая стоимость, ₽: 49400982
Дата определения стоимости: 2022-01-01
Дата постановки на учёт: 2024-09-05
Кадастровый квартал: 28:10:000000
Статус: Учтённый"""


def windows():
    from GeoSearchRU.geosearch_ru_dialog import GeoSearchDialog
    from GeoSearchRU.keys_dialog import KeysDialog
    from GeoSearchRU.providers import Dadata, Nominatim, Yandex

    iface = Iface()

    def make(provider=Dadata()):
        dialog = GeoSearchDialog(SOURCES, iface.mainWindow())
        dialog.show_provider(provider)
        return dialog

    def empty():
        return make()

    def address_found():
        dialog = make(Nominatim())
        dialog.address_edit.setText("Тамбов, Энергетиков 7")
        dialog.set_results([
            "392000, Тамбовская обл, г Тамбов, пр-кт Энергетиков, д 7 — дом",
            "Тамбовская обл, г Тамбов, пр-кт Энергетиков — улица",
            "Тамбовская обл, г Тамбов — город или крупнее",
        ])
        dialog.normalized_address.setPlainText(ADDRESS_CARD)
        dialog.add_point_button.setEnabled(True)
        dialog.set_status("Адрес и координаты найдены.")
        return dialog

    def cadastre_found():
        dialog = make()
        dialog.cadastral_edit.setText("28:10:000000:7270")
        dialog.set_results([
            "28:10:000000:7270 — Земельные участки ЕГРН, Российская Федерация, Амурская область, "
            "муниципальный округ Зейский, посёлок городского типа Верхнезейск"])
        dialog.normalized_address.setPlainText(CADASTRE_CARD)
        dialog.show_add_mode(True)
        dialog.add_point_button.setEnabled(True)
        dialog.set_status("Объект ЕГРН найден, границы показаны на карте.")
        return dialog

    def error():
        dialog = make(Yandex())
        dialog.cadastral_edit.setText("28:10:000000:7270")
        dialog.set_status(
            "НСПД (ЕГРН) разорвал защищённое соединение. Он доступен только из России: "
            "проверьте подключение и отключите VPN, если он включён.", "error")
        return dialog

    def busy():
        dialog = make()
        dialog.address_edit.setText("Тамбов, Энергетиков 7")
        dialog.set_busy(True)
        return dialog

    def keys():
        return KeysDialog([Dadata(), Yandex()], {"dadata": ["", ""], "yandex": ["", ""]},
                          True, iface.mainWindow())

    return [
        ("поиск_пустое", empty),
        ("поиск_адрес_найден", address_found),
        ("поиск_кадастр_найден", cadastre_found),
        ("поиск_ошибка", error),
        ("поиск_идёт_запрос", busy),
        ("ключи", keys),
    ]
