#!/bin/sh
# Запуск проверок на Python из QGIS для macOS.
# Без аргумента — tests/test_plugin.py; tests/run_tests.sh live_yandex.py — живая проверка Яндекса.
# Другой QGIS: QGIS_APP=/Applications/QGIS-final-4_2_2.app tests/run_tests.sh
set -e
cd "$(dirname "$0")"
APP="${QGIS_APP:-}"
if [ -z "$APP" ]; then
  for a in /Applications/QGIS-LTR.app /Applications/QGIS.app \
           $(ls -d /Applications/QGIS-3*.app 2>/dev/null | sort -V | tail -1); do
    [ -d "$a" ] && APP="$a" && break
  done
fi
[ -d "$APP" ] || { echo "QGIS не найден, укажите QGIS_APP=/путь/к/QGIS.app"; exit 1; }
C="$APP/Contents"
# Старая сборка: свой python в MacOS/bin и пакеты в Resources/python.
# Сборка 3.44 и новее: python рядом с QGIS, пакеты в Resources/python<версия>/site-packages,
# стандартная библиотека — в Frameworks, её путь надо задать через PYTHONHOME.
PY="$C/MacOS/bin/python3"
if [ ! -x "$PY" ]; then
  PY=$(ls "$C/MacOS"/python3.* 2>/dev/null | head -1)
  export PYTHONHOME="$C/Frameworks"
fi
[ -x "$PY" ] || { echo "в $APP нет python"; exit 1; }
SITE=$(ls -d "$C/Resources"/python*/site-packages 2>/dev/null | head -1)
export QGIS_PREFIX_PATH="$C/MacOS"
export PYTHONPATH="${SITE:-$C/Resources/python}${PYTHONPATH:+:$PYTHONPATH}"
# proj.db переехал: Resources/proj в старых сборках, Resources/qgis/proj в 3.44 и новее.
# Без него пересчёт координат молча возвращает исходную точку.
PROJ="$C/Resources/proj"
[ -f "$PROJ/proj.db" ] || PROJ="$C/Resources/qgis/proj"
[ -f "$PROJ/proj.db" ] || { echo "в $APP не найден proj.db"; exit 1; }
export PROJ_DATA="$PROJ" PROJ_LIB="$PROJ"
export QT_QPA_PLATFORM=offscreen
set +e
"$PY" "${1:-test_plugin.py}" > _out.log 2>&1
STATUS=$?
grep -v "proj_create_from_database\|Cannot find proj.db\|propagateSizeHints" _out.log
rm -f _out.log
exit $STATUS
