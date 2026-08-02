"""Ozon category catalog collector package."""

from .models import CategoryNode, LoadStats, StructureChange

__all__ = [
    "CategoryNode",
    "HttpxPageFetcher",
    "LoadStats",
    "OzonCategoryCollector",
    "PlaywrightPageFetcher",
    "StructureChange",
]


def __getattr__(name: str):
    if name in {"HttpxPageFetcher", "OzonCategoryCollector", "PlaywrightPageFetcher"}:
        from . import collector

        return getattr(collector, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
