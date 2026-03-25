"""Report generation module.

Provides formatted text representations of work logs for daily, weekly, and
monthly periods.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from .storage import WorkLogStorage

_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class ReportGenerator:
    """Generates human-readable work log reports."""

    def __init__(self, storage: WorkLogStorage) -> None:
        self._storage = storage

    # ------------------------------------------------------------------
    # Internal formatters (raw text, no LLM)
    # ------------------------------------------------------------------

    def _format_daily_raw(self, logs: list[dict[str, Any]], report_date: date) -> str:
        date_str = report_date.strftime("%Y年%m月%d日") + " " + _WEEKDAY_NAMES[report_date.weekday()]
        lines = [f"📅 {date_str} 工作日报", ""]
        if not logs:
            lines.append("暂无工作记录。")
            return "\n".join(lines)

        # Display entries in flat chronological order with globally sequential numbers.
        # This makes the displayed index identical to the delete index — no gaps.
        for global_idx, entry in enumerate(logs, start=1):
            time_part = entry.get("timestamp", "")[:19][11:16]
            name = entry.get("sender_name") or entry.get("sender_id", "")
            prefix = f"[{name}] " if name else ""
            lines.append(f"  {global_idx}. [{time_part}] {prefix}{entry.get('content', '')}")
        return "\n".join(lines).rstrip()

    def _format_weekly_raw(
        self,
        logs_by_date: dict[str, list[dict[str, Any]]],
        week_start: date,
        week_end: date,
    ) -> str:
        start_str = week_start.strftime("%Y年%m月%d日")
        end_str = week_end.strftime("%m月%d日")
        lines = [f"📊 {start_str}—{end_str} 工作周报", ""]

        if not logs_by_date:
            lines.append("本周暂无工作记录。")
            return "\n".join(lines)

        total = sum(len(v) for v in logs_by_date.values())
        lines.append(f"本周共记录 {total} 条工作内容，合计工作 {len(logs_by_date)} 天。")
        lines.append("")

        for date_key in sorted(logs_by_date):
            try:
                d = date.fromisoformat(date_key)
            except ValueError:
                continue
            weekday = _WEEKDAY_NAMES[d.weekday()]
            lines.append(f"📌 {d.strftime('%m月%d日')}（{weekday}）")
            for entry in logs_by_date[date_key]:
                time_part = entry.get("timestamp", "")[:19][11:16]
                name = entry.get("sender_name") or ""
                prefix = f"{name}: " if name else ""
                lines.append(f"  • [{time_part}] {prefix}{entry.get('content', '')}")
            lines.append("")

        return "\n".join(lines).rstrip()

    def _format_monthly_raw(
        self,
        logs_by_date: dict[str, list[dict[str, Any]]],
        year: int,
        month: int,
    ) -> str:
        month_str = f"{year}年{month:02d}月"
        lines = [f"📈 {month_str} 工作月报", ""]

        if not logs_by_date:
            lines.append("本月暂无工作记录。")
            return "\n".join(lines)

        total = sum(len(v) for v in logs_by_date.values())
        lines.append(
            f"本月共记录 {total} 条工作内容，合计工作 {len(logs_by_date)} 天。"
        )
        lines.append("")

        # Weekly breakdown within the month
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        week_num = 1
        cur = first_day
        while cur <= last_day:
            week_end_cur = min(cur + timedelta(days=6 - cur.weekday()), last_day)
            week_logs = {
                k: v
                for k, v in logs_by_date.items()
                if cur.isoformat() <= k <= week_end_cur.isoformat()
            }
            if week_logs:
                week_total = sum(len(v) for v in week_logs.values())
                lines.append(
                    f"── 第{week_num}周（{cur.strftime('%m/%d')}—{week_end_cur.strftime('%m/%d')}）"
                    f"  共 {week_total} 条"
                )
                for date_key in sorted(week_logs):
                    try:
                        d = date.fromisoformat(date_key)
                    except ValueError:
                        continue
                    weekday = _WEEKDAY_NAMES[d.weekday()]
                    lines.append(f"   {d.strftime('%m/%d')} {weekday}: {len(week_logs[date_key])} 条记录")
                    for entry in week_logs[date_key]:
                        time_part = entry.get("timestamp", "")[:19][11:16]
                        name = entry.get("sender_name") or ""
                        prefix = f"{name}: " if name else ""
                        lines.append(f"     • [{time_part}] {prefix}{entry.get('content', '')}")
                lines.append("")
            # Advance to next Monday
            days_to_monday = 7 - cur.weekday()
            cur = cur + timedelta(days=days_to_monday)
            week_num += 1

        return "\n".join(lines).rstrip()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_daily_raw(
        self,
        sessions: list[str],
        report_date: date | None = None,
    ) -> tuple[str, bool]:
        """Return (raw_text, has_data) for the daily report."""
        if report_date is None:
            report_date = date.today()
        logs = self._storage.get_logs_by_date_multi(sessions, report_date)
        text = self._format_daily_raw(logs, report_date)
        return text, bool(logs)

    def get_weekly_raw(
        self,
        sessions: list[str],
        week_start: date | None = None,
    ) -> tuple[str, bool]:
        """Return (raw_text, has_data) for the weekly report.

        *week_start* defaults to the start of the current week (Monday).
        """
        if week_start is None:
            today = date.today()
            week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        logs_by_date = self._storage.get_logs_by_range_multi(sessions, week_start, week_end)
        text = self._format_weekly_raw(logs_by_date, week_start, week_end)
        return text, bool(logs_by_date)

    def get_monthly_raw(
        self,
        sessions: list[str],
        year: int | None = None,
        month: int | None = None,
    ) -> tuple[str, bool]:
        """Return (raw_text, has_data) for the monthly report."""
        if year is None or month is None:
            today = date.today()
            year, month = today.year, today.month
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        logs_by_date = self._storage.get_logs_by_range_multi(sessions, first_day, last_day)
        text = self._format_monthly_raw(logs_by_date, year, month)
        return text, bool(logs_by_date)
