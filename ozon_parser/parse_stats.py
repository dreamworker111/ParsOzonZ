from dataclasses import dataclass, field
from time import monotonic


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} сек"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} мин {secs} сек"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин {secs} сек"


@dataclass
class ParseStatus:
    active_section: str = ""
    active_category: str = ""
    active_name: str = ""
    current_index: int = 0
    total_count: int = 0
    total_elapsed_sec: float = 0.0
    section_elapsed_sec: float = 0.0
    message: str = ""


@dataclass
class SectionTiming:
    section: str
    category: str
    name: str
    index: int
    total: int
    duration_sec: float
    products_found: int

    @property
    def duration_fmt(self) -> str:
        return format_duration(self.duration_sec)

    def summary_line(self) -> str:
        cat = f"{self.category} → " if self.category and self.category != self.name else ""
        return (
            f"{self.index}/{self.total} | {self.section}: {cat}{self.name} — "
            f"{self.products_found} тов., {self.duration_fmt}"
        )


@dataclass
class ParseStats:
    section_timings: list[SectionTiming] = field(default_factory=list)
    total_duration_sec: float = 0.0

    @property
    def total_duration_fmt(self) -> str:
        return format_duration(self.total_duration_sec)

    def summary_text(self) -> str:
        if not self.section_timings:
            return "—"
        lines = [t.summary_line() for t in self.section_timings]
        lines.append(f"Итого: {self.total_duration_fmt}")
        return "\n".join(lines)


class ParseTimer:
    def __init__(self) -> None:
        self._started_at = monotonic()
        self._section_started_at = monotonic()

    def reset(self) -> None:
        self._started_at = monotonic()
        self._section_started_at = monotonic()

    def restart_section(self) -> None:
        self._section_started_at = monotonic()

    @property
    def total_elapsed(self) -> float:
        return monotonic() - self._started_at

    @property
    def section_elapsed(self) -> float:
        return monotonic() - self._section_started_at
