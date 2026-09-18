#!/bin/sh
# Запуск проверок на Python из QGIS для macOS (QGIS-LTR.app или QGIS.app).
# Другой путь к приложению: QGIS_APP=/Applications/QGIS-3.44.app tests/run_tests.sh
set -e
cd "$(dirname "$0")"
APP="${QGIS_APP:-}"
if [ -z "$APP" ]; then
  for a in /Applications/QGIS-LTR.app /Applications/QGIS.app; do
    [ -d "$a" ] && APP="$a" && break
  done
fi
[ -d "$APP" ] || { echo "QGIS не найден, укажите QGIS_APP=/путь/к/QGIS.app"; exit 1; }
C="$APP/Contents"
export QGIS_PREFIX_PATH="$C/MacOS"
export PYTHONPATH="$C/Resources/python${PYTHONPATH:+:$PYTHONPATH}"
export PROJ_DATA="$C/Resources/proj" PROJ_LIB="$C/Resources/proj"
export QT_QPA_PLATFORM=offscreen
set +e
"$C/MacOS/bin/python3" test_plugin.py > _out.log 2>&1
STATUS=$?
grep -v "proj_create_from_database\|Cannot find proj.db\|propagateSizeHints" _out.log
rm -f _out.log
exit $STATUS
