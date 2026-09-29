"""Живая проверка поиска по кадастровому номеру в НСПД — не входит в run_tests.sh.

Один запрос к настоящему геопорталу тем же сетевым стеком, которым ходит модуль
(Qt, а не urllib: у Python своё хранилище сертификатов, и проверять надо не его):
    tests/run_tests.sh live_nspd.py                 # участок в Тамбове
    tests/run_tests.sh live_nspd.py 68:29:0309001:24

Ключей не требует. НСПД доступен только из России и работает по сертификату
Минцифры: проверка подлинности сервера здесь не отключается, как и в модуле.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, QSettings, QTimer  # noqa: E402

QSettings.setDefaultFormat(QSettings.Format.IniFormat)
PROFILE = os.path.join(HERE, "_profile")
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
QCoreApplication.setOrganizationName("geosearch-ru-tests")
QCoreApplication.setApplicationName("geosearch-ru-tests")

from qgis.core import QgsApplication  # noqa: E402
from qgis.PyQt.QtNetwork import (  # noqa: E402
    QNetworkAccessManager, QNetworkReply, QNetworkRequest, QSslCertificate, QSslConfiguration,
)

APP = QgsApplication([], False)
APP.initQgis()
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)

from GeoSearchRU.nspd import Nspd  # noqa: E402

NUMBER = sys.argv[1] if len(sys.argv) > 1 else "68:29:0309001:24"
# В tests/_profile мог остаться тестовый сервер из test_plugin.py — здесь нужен настоящий.
QSettings().remove(Nspd.SETTINGS_URL)
source = Nspd()
request, _body = source.request(NUMBER)
request.setTransferTimeout(Nspd.TIMEOUT_MS)
print(request.url().toString())
# У НСПД сертификат Минцифры: без его корня Qt оборвёт соединение.
CERTIFICATES = QSslConfiguration.defaultConfiguration().caCertificates()
RUSSIAN_ROOT = any(
    "Russian Trusted Root" in name
    for certificate in CERTIFICATES
    for name in certificate.subjectInfo(QSslCertificate.SubjectInfo.CommonName))
print(f"корневых сертификатов у Qt: {len(CERTIFICATES)}, "
      f"корень Минцифры: {'есть' if RUSSIAN_ROOT else 'НЕТ'}\n")

manager = QNetworkAccessManager()
reply = manager.get(request)
problems = []
reply.sslErrors.connect(lambda errors: problems.extend(e.errorString() for e in errors))
loop = QEventLoop()
reply.finished.connect(loop.quit)
QTimer.singleShot(Nspd.TIMEOUT_MS + 5000, loop.quit)
loop.exec()

status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
if problems:
    print("Сертификат сервера не принят:")
    for problem in problems:
        print(f"  {problem}")
    print("\nНужен корневой сертификат Минцифры в том хранилище, которым пользуется QGIS:\n"
          "«Настройки → Параметры → Аутентификация → Центры сертификации» (или модуль certificat_mincif).")
    APP.exitQgis()
    sys.exit(1)
if reply.error() != QNetworkReply.NetworkError.NoError and status is None:
    print(f"НСПД не ответил: {reply.errorString()}.\n{Nspd.TIMEOUT_HINT}")
    APP.exitQgis()
    sys.exit(1)

body = bytes(reply.readAll()).decode("utf-8", errors="replace")
if status is not None and int(status) != 200:
    print(f"НСПД ответил {status}: {Nspd.HTTP_HINTS.get(int(status), reply.errorString())}")
    print(body[:300])
    APP.exitQgis()
    sys.exit(1)

results = source.parse(body)
print(f"объектов: {len(results)}")
for result in results:
    where = "без границ" if result.latitude is None else f"{result.latitude:.6f}, {result.longitude:.6f}"
    print(f"- {result.address} ({where}, {result.precision})")
    for line in (result.details or "").splitlines():
        print(f"    {line}")
    if result.geometry:
        wkt = result.geometry[0]
        print(f"    границы: {wkt[:90]}…" if len(wkt) > 90 else f"    границы: {wkt}")
APP.exitQgis()
sys.exit(0 if results else 1)
