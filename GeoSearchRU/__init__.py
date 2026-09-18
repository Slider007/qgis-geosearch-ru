"""QGIS plugin entry point for the «Поиск адреса» plugin (GeoSearchRU)."""


def classFactory(iface):
    from .geosearch_ru import GeoSearchRU
    return GeoSearchRU(iface)
