#!/bin/sh
# Собирает dist/GeoSearchRU-<версия>.zip для «Модули → Установить из ZIP».
set -e
cd "$(dirname "$0")"
VERSION=$(sed -n 's/^version=//p' GeoSearchRU/metadata.txt)
mkdir -p dist
ZIP="dist/GeoSearchRU-$VERSION.zip"
rm -f "$ZIP"
zip -qr "$ZIP" GeoSearchRU -x '*__pycache__*' '*.pyc' '*.DS_Store'
echo "$ZIP"
