"""QGIS plugin entry point for GeoSearch RU."""


def classFactory(iface):
    from .geosearch_ru import GeoSearchRU
    return GeoSearchRU(iface)
