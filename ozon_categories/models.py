"""Domain models for Ozon category tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class CategoryNode:
    """Single node in Ozon category catalog."""

    id: str
    name: str
    url: str | None = None
    parent_id: str | None = None
    level: int = 0
    path: str = ""
    children: list[CategoryNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "parent_id": self.parent_id,
            "level": self.level,
            "path": self.path,
            "children": [child.to_dict() for child in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CategoryNode:
        children = [cls.from_dict(item) for item in data.get("children", [])]
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            url=data.get("url"),
            parent_id=data.get("parent_id"),
            level=int(data.get("level", 0)),
            path=str(data.get("path", "")),
            children=children,
        )


@dataclass(slots=True)
class StructureChange:
    """Detected difference between cached and fresh category trees."""

    kind: str
    category_id: str
    message: str


@dataclass(slots=True)
class LoadStats:
    """Statistics for a category load/update run."""

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    source_url: str = ""
    roots_count: int = 0
    categories_total: int = 0
    max_depth: int = 0
    errors_count: int = 0
    retries_count: int = 0
    requests_count: int = 0
    from_cache: bool = False
    structure_changed: bool = False
    duration_sec: float = 0.0
    changes: list[StructureChange] = field(default_factory=list)

    def finish(self) -> None:
        self.finished_at = datetime.now(timezone.utc)
        if self.started_at and self.finished_at:
            self.duration_sec = (self.finished_at - self.started_at).total_seconds()


@dataclass(slots=True)
class CachePayload:
    """Serialized cache entry."""

    fingerprint: str
    updated_at: str
    source_url: str
    roots: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "updated_at": self.updated_at,
            "source_url": self.source_url,
            "roots": self.roots,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CachePayload:
        return cls(
            fingerprint=str(data.get("fingerprint", "")),
            updated_at=str(data.get("updated_at", "")),
            source_url=str(data.get("source_url", "")),
            roots=list(data.get("roots", [])),
        )
