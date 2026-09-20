"""Живая проверка «Что здесь?» на настоящем Overpass — не входит в run_tests.sh.

Один запрос к общественному серверу по точке из аргумента (по умолчанию — Тамбов):
    tests/run_tests.sh live_overpass.py
Ключей не требует. Тратит один запрос из суточного лимита сервера.
"""

import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402

QSettings.setDefaultFormat(QSettings.Format.IniFormat)
PROFILE = os.path.join(HERE, "_profile")
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
QCoreApplication.setOrganizationName("geosearch-ru-tests")
QCoreApplication.setApplicationName("geosearch-ru-tests")

from qgis.core import QgsApplication  # noqa: E402

APP = QgsApplication([], False)
APP.initQgis()
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)

from GeoSearchRU.overpass import Overpass  # noqa: E402

LATITUDE, LONGITUDE = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) > 2 else (52.7201, 41.4502)
# В tests/_profile мог остаться тестовый сервер из test_plugin.py — здесь нужен настоящий.
QSettings().remove(Overpass.SETTINGS_URL)
source = Overpass()
request, body = source.reverse(LATITUDE, LONGITUDE)
url = request.url().toString()
print(f"{url}\n{source.query(LATITUDE, LONGITUDE)}\n")
# QgsNetworkAccessManager здесь не нужен: проверяется запрос и разбор ответа, а не настройки сети.
http = urllib.request.Request(url, data=body, headers={
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": "https://github.com/Slider007/qgis-geosearch-ru",
    "User-Agent": "Mozilla/5.0 QGIS/geosearch-ru live check",
})
with urllib.request.urlopen(http, timeout=60) as answer:
    text = answer.read().decode("utf-8")
results = source.parse(text)
print(f"объектов: {len(results)}")
for result in results:
    coordinates = "без координат" if result.latitude is None else f"{result.latitude:.5f}, {result.longitude:.5f}"
    print(f"- {result.address} ({coordinates})")
    for line in (result.details or "").splitlines():
        print(f"    {line}")
APP.exitQgis()
sys.exit(0 if results else 1)
