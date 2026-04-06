"""セールカレンダー管理モジュール."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Optional


@dataclass
class SaleEvent:
    name: str
    start_date: str   # YYYY-MM-DD
    end_date: str     # YYYY-MM-DD
    multiplier: float = 2.6   # 通常日比の販売倍率


class SaleCalendar:
    """セールイベントの読み込み・検索・保存."""

    DEFAULT_PATH = Path("data/sale_events.json")

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or self.DEFAULT_PATH
        self._events: list[SaleEvent] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._events = [SaleEvent(**e) for e in data.get("events", [])]

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"events": [asdict(e) for e in self._events]}
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def add_event(self, event: SaleEvent) -> None:
        self._events.append(event)
        self.save()

    def remove_event(self, name: str) -> bool:
        before = len(self._events)
        self._events = [e for e in self._events if e.name != name]
        if len(self._events) < before:
            self.save()
            return True
        return False

    def list_events(self) -> list[SaleEvent]:
        return sorted(self._events, key=lambda e: e.start_date)

    def get_multiplier(self, d: date) -> float:
        """指定日の販売倍率を返す（通常日は1.0）."""
        ds = d.strftime("%Y-%m-%d")
        for event in self._events:
            if event.start_date <= ds <= event.end_date:
                return event.multiplier
        return 1.0

    def is_sale_day(self, d: date) -> bool:
        return self.get_multiplier(d) > 1.0

    def projected_consumption(self, daily_normal_rate: float, days: int = 30,
                               start: Optional[date] = None) -> float:
        """今後 days 日間の予測消費量（セール日を考慮）."""
        start = start or date.today()
        total = 0.0
        for i in range(days):
            d = start + timedelta(days=i)
            total += daily_normal_rate * self.get_multiplier(d)
        return total

    def projected_days_remaining(self, stock: int, daily_normal_rate: float,
                                  max_days: int = 365) -> float:
        """在庫が尽きるまでの予測日数（セール日を考慮）."""
        if daily_normal_rate <= 0:
            return float("inf")
        start = date.today()
        consumed = 0.0
        for i in range(max_days):
            d = start + timedelta(days=i)
            consumed += daily_normal_rate * self.get_multiplier(d)
            if consumed >= stock:
                return float(i + 1)
        return float(max_days)


def group_sale_dates(dates: list[str], gap_days: int = 3,
                     multiplier: float = 2.6) -> list[SaleEvent]:
    """連続するセール日をグループ化してSaleEventリストを返す."""
    if not dates:
        return []
    sorted_dates = sorted(dates)
    events: list[SaleEvent] = []
    group_start = sorted_dates[0]
    group_end = sorted_dates[0]

    for ds in sorted_dates[1:]:
        prev = date.fromisoformat(group_end)
        curr = date.fromisoformat(ds)
        if (curr - prev).days <= gap_days:
            group_end = ds
        else:
            events.append(_name_event(group_start, group_end, multiplier))
            group_start = ds
            group_end = ds

    events.append(_name_event(group_start, group_end, multiplier))
    return events


_SEASON_NAMES = {
    "01": "冬セール", "02": "冬セール", "03": "春セール",
    "04": "春セール", "05": "初夏セール", "06": "夏セール",
    "07": "プライムデー", "08": "夏セール", "09": "秋セール",
    "10": "秋セール", "11": "ブラックフライデー", "12": "年末セール",
}


def _name_event(start: str, end: str, multiplier: float) -> SaleEvent:
    month = start[5:7]
    year = start[:4]
    name = f"{year}年{_SEASON_NAMES.get(month, 'セール')}({start}~{end})"
    return SaleEvent(name=name, start_date=start, end_date=end, multiplier=multiplier)
