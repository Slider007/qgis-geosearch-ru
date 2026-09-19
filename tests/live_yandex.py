"""Живая проверка Яндекс Геокодера с настоящим ключом — не входит в run_tests.sh.

Ключ и секрет берутся из yandex.local.json в корне репозитория (файл в .gitignore):
    {"key": "…", "secret": "…"}   — секрет можно оставить пустым.
Запуск: tests/run_tests.sh live_yandex.py
Тратит 1–3 запроса из суточного лимита.
"""

import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402

# Settings go to a throwaway profile, never to the user's QGIS profile.
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
PROFILE = os.path.join(HERE, "_profile")
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
QCoreApplication.setOrganizationName("geosearch-ru-tests")
QCoreApplication.setApplicationName("geosearch-ru-tests")

from qgis.core import QgsApplication  # noqa: E402

APP = QgsApplication([], False)
APP.initQgis()
# initQgis() moves settings into ~/Library/Application Support/<organisation>/…/profiles/default,
# which is never cleaned up: put them back into the throwaway profile.
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
assert QSettings().fileName().startswith(PROFILE), QSettings().fileName()

from GeoSearchRU.providers import Yandex  # noqa: E402

with open(os.path.join(os.path.dirname(HERE), "yandex.local.json"), encoding="utf-8") as f:
    creds = json.load(f)
key, secret = creds.get("key", "").strip(), creds.get("secret", "").strip()
if not key:
    sys.exit("В yandex.local.json не указан key")


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def url_for(mode):
    QSettings().setValue(Yandex.SETTINGS_SIGNATURE, mode)
    request, _ = Yandex().request("Тамбов, проспект Энергетиков, 7", key, 3, secret if mode != "none" else "")
    return bytes(request.url().toEncoded()).decode()


modes = ["ttl", "plain", "none"] if secret else ["none"]
ok = False
for mode in modes:
    status, body = fetch(url_for(mode))
    print(f"подпись {mode}: HTTP {status}")
    if status == 200:
        for result in Yandex().parse(body):
            print(f"  {result.address} — {result.precision} ({result.latitude}, {result.longitude})")
        ok = True
        break
    print("  ", body[:200])
APP.exitQgis()
sys.exit(0 if ok else 1)
