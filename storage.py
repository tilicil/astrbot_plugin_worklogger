"""Work log storage module.

Manages persistent storage of work log entries using monthly JSON files,
organized per session.

File structure:
    <plugin_data>/work_logger/
        sessions.json            -- maps session_id -> directory hash
        groups.json              -- maps group_name -> [session_id, ...]
        logs/
            <hash>/
                <YYYY-MM>.json   -- monthly data: {date_str: [entries]}
"""

import asyncio
import hashlib
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger

_LOG_ENTRY_KEYS = ("timestamp", "sender_id", "sender_name", "content")

# Regex that matches directory hashes produced by _session_hash().
# Accepts both the legacy 16-char (SHA-1) and current 32-char (SHA-256) formats
# to maintain backward compatibility when upgrading from older plugin versions.
_VALID_HASH_RE = re.compile(r"^[0-9a-f]{16}$|^[0-9a-f]{32}$")

MAX_CONTENT_LEN = 2000


def _session_hash(session: str) -> str:
    """Return a filesystem-safe directory name derived from a session ID.

    Uses the first 32 hex characters of SHA-256 (128 bits) to keep
    collision probability negligible even with large numbers of sessions.
    """
    return hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]


def _save_json_atomic(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically via a temp-file rename.

    Prevents half-written files from corrupting stored data if the process
    is interrupted mid-write.
    """
    tmp_path = path.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


class WorkLogStorage:
    """Stores and retrieves work log entries organized by session and date."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir = self._data_dir / "logs"
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_file = self._data_dir / "sessions.json"
        self._session_map: dict[str, str] = self._load_session_map()
        self._groups_file = self._data_dir / "groups.json"
        self._group_map: dict[str, list[str]] = self._load_groups()
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_session_map(self) -> dict[str, str]:
        if self._sessions_file.exists():
            try:
                with self._sessions_file.open(encoding="utf-8") as fh:
                    data = json.load(fh)
                # Validate hash values to prevent path traversal attacks.
                return {
                    k: v
                    for k, v in data.items()
                    if isinstance(k, str)
                    and isinstance(v, str)
                    and _VALID_HASH_RE.match(v)
                }
            except Exception as exc:
                logger.warning(f"[work_logger] Failed to load sessions.json: {exc}")
        return {}

    def _save_session_map(self) -> None:
        try:
            _save_json_atomic(self._sessions_file, self._session_map)
        except Exception as exc:
            logger.error(f"[work_logger] Failed to save sessions.json: {exc}")

    def _load_groups(self) -> dict[str, list[str]]:
        if self._groups_file.exists():
            try:
                with self._groups_file.open(encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    return {
                        k: [s for s in v if isinstance(s, str)]
                        for k, v in data.items()
                        if isinstance(k, str) and isinstance(v, list)
                    }
            except Exception as exc:
                logger.warning(f"[work_logger] Failed to load groups.json: {exc}")
        return {}

    def _save_groups(self) -> None:
        try:
            _save_json_atomic(self._groups_file, self._group_map)
        except Exception as exc:
            logger.error(f"[work_logger] Failed to save groups.json: {exc}")

    def _ensure_session(self, session: str) -> str:
        """Return the directory hash for *session*, creating the mapping if needed."""
        if session not in self._session_map:
            h = _session_hash(session)
            self._session_map[session] = h
            self._save_session_map()
        return self._session_map[session]

    def _monthly_file(self, session: str, year: int, month: int) -> Path:
        h = self._ensure_session(session)
        session_dir = self._logs_dir / h
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir / f"{year:04d}-{month:02d}.json"

    def _load_monthly(self, session: str, year: int, month: int) -> dict[str, list[dict[str, Any]]]:
        path = self._monthly_file(session, year, month)
        if not path.exists():
            return {}
        try:
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {}
            # Validate and filter the nested structure to guard against
            # corrupted or tampered storage files.
            result: dict[str, list[dict[str, Any]]] = {}
            for date_key, entries in data.items():
                if not isinstance(date_key, str):
                    continue
                try:
                    date.fromisoformat(date_key)
                except ValueError:
                    continue
                if not isinstance(entries, list):
                    continue
                valid_entries = [
                    e for e in entries
                    if isinstance(e, dict) and isinstance(e.get("content"), str)
                ]
                if valid_entries:
                    result[date_key] = valid_entries
            return result
        except Exception as exc:
            logger.error(f"[work_logger] Failed to read {path}: {exc}")
            return {}

    def _save_monthly(
        self,
        session: str,
        year: int,
        month: int,
        data: dict[str, list[dict[str, Any]]],
    ) -> None:
        path = self._monthly_file(session, year, month)
        try:
            _save_json_atomic(path, data)
        except Exception as exc:
            logger.error(f"[work_logger] Failed to write {path}: {exc}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_log(
        self,
        session: str,
        sender_id: str,
        sender_name: str,
        content: str,
    ) -> None:
        """Append a work log entry for today.

        Protected by an asyncio lock to prevent concurrent read-modify-write races.
        """
        from datetime import datetime

        content = content[:MAX_CONTENT_LEN]
        today = date.today()
        async with self._write_lock:
            monthly = self._load_monthly(session, today.year, today.month)
            date_key = today.isoformat()
            if date_key not in monthly:
                monthly[date_key] = []
            entry: dict[str, Any] = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "sender_id": sender_id,
                "sender_name": sender_name,
                "content": content,
            }
            monthly[date_key].append(entry)
            self._save_monthly(session, today.year, today.month, monthly)

    def get_logs_by_date(self, session: str, log_date: date) -> list[dict[str, Any]]:
        """Return all log entries for *session* on *log_date*."""
        monthly = self._load_monthly(session, log_date.year, log_date.month)
        return monthly.get(log_date.isoformat(), [])

    def get_logs_by_range(
        self,
        session: str,
        start: date,
        end: date,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return all log entries for *session* between *start* and *end* (inclusive).

        Returns a dict keyed by ISO date string (YYYY-MM-DD), containing only
        dates that have at least one entry.
        """
        # Collect the set of (year, month) pairs we need.
        months: set[tuple[int, int]] = set()
        cur = start
        while cur <= end:
            months.add((cur.year, cur.month))
            # Advance to the first day of the next month to minimise iterations.
            if cur.month == 12:
                cur = date(cur.year + 1, 1, 1)
            else:
                cur = date(cur.year, cur.month + 1, 1)

        combined: dict[str, list[dict[str, Any]]] = {}
        for year, month in sorted(months):
            monthly = self._load_monthly(session, year, month)
            for date_key, entries in monthly.items():
                try:
                    d = date.fromisoformat(date_key)
                except ValueError:
                    continue
                if start <= d <= end and entries:
                    combined[date_key] = entries

        return dict(sorted(combined.items()))

    async def delete_log(self, session: str, log_date: date, entry_index: int) -> bool:
        """Delete a log entry by 1-based *entry_index* for the given date.

        Protected by an asyncio lock to prevent concurrent read-modify-write races.
        Returns True on success, False when the index is out of range or the
        date has no entries.
        """
        if entry_index < 1:
            return False
        async with self._write_lock:
            monthly = self._load_monthly(session, log_date.year, log_date.month)
            date_key = log_date.isoformat()
            entries = monthly.get(date_key, [])
            if not entries or entry_index > len(entries):
                return False
            entries.pop(entry_index - 1)
            if entries:
                monthly[date_key] = entries
            else:
                del monthly[date_key]
            self._save_monthly(session, log_date.year, log_date.month, monthly)
            return True

    def get_all_sessions(self) -> list[str]:
        """Return all known session IDs that have ever recorded logs."""
        return list(self._session_map.keys())

    # ------------------------------------------------------------------
    # Session group management
    # ------------------------------------------------------------------

    def get_group_for_session(self, session: str) -> str | None:
        """Return the group name the session belongs to, or None."""
        for group_name, members in self._group_map.items():
            if session in members:
                return group_name
        return None

    def get_group_sessions(self, group_name: str) -> list[str]:
        """Return all session IDs in the given group."""
        return list(self._group_map.get(group_name, []))

    def get_all_groups(self) -> dict[str, list[str]]:
        """Return a snapshot of the full group map."""
        return dict(self._group_map)

    def create_group(self, group_name: str) -> bool:
        """Create a new empty group.  Returns False if the name already exists."""
        if group_name in self._group_map:
            return False
        self._group_map[group_name] = []
        self._save_groups()
        return True

    def add_to_group(self, group_name: str, session: str) -> tuple[bool, str]:
        """Add *session* to *group_name*, auto-creating the group if needed.

        Returns (True, "") on success.
        Returns (False, reason) when the session is already in another group,
        or already in this group.
        """
        existing = self.get_group_for_session(session)
        if existing == group_name:
            return False, f"会话已在组 '{group_name}' 中"
        if existing is not None:
            return False, f"会话已在组 '{existing}' 中，请先使用 /worklog group leave 退出"
        self._group_map.setdefault(group_name, []).append(session)
        self._save_groups()
        return True, ""

    def remove_from_group(self, session: str) -> tuple[bool, str]:
        """Remove *session* from whichever group it belongs to.

        Returns (True, group_name) on success, (False, "") if not in any group.
        Empty groups are automatically deleted.
        """
        group_name = self.get_group_for_session(session)
        if group_name is None:
            return False, ""
        self._group_map[group_name].remove(session)
        if not self._group_map[group_name]:
            del self._group_map[group_name]
        self._save_groups()
        return True, group_name

    def delete_group(self, group_name: str) -> bool:
        """Delete an entire group.  Returns False if the group does not exist."""
        if group_name not in self._group_map:
            return False
        del self._group_map[group_name]
        self._save_groups()
        return True

    def resolve_sessions(self, session: str) -> list[str]:
        """Return the list of sessions whose logs should be queried together.

        If *session* belongs to a group, returns all sessions in that group.
        Otherwise returns [session].
        """
        group = self.get_group_for_session(session)
        if group:
            members = self.get_group_sessions(group)
            if members:
                return members
        return [session]

    # ------------------------------------------------------------------
    # Multi-session query helpers
    # ------------------------------------------------------------------

    def get_logs_by_date_multi(
        self,
        sessions: list[str],
        log_date: date,
    ) -> list[dict[str, Any]]:
        """Return merged log entries for all *sessions* on *log_date*, sorted by timestamp."""
        all_entries: list[dict[str, Any]] = []
        for session in sessions:
            all_entries.extend(self.get_logs_by_date(session, log_date))
        all_entries.sort(key=lambda e: e.get("timestamp", ""))
        return all_entries

    def get_logs_by_range_multi(
        self,
        sessions: list[str],
        start: date,
        end: date,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return merged log entries for all *sessions* between *start* and *end*.

        Entries within each date are sorted by timestamp.
        """
        combined: dict[str, list[dict[str, Any]]] = {}
        for session in sessions:
            for date_key, entries in self.get_logs_by_range(session, start, end).items():
                combined.setdefault(date_key, []).extend(entries)
        for entries in combined.values():
            entries.sort(key=lambda e: e.get("timestamp", ""))
        return dict(sorted(combined.items()))

    def resolve_global_entry(
        self,
        sessions: list[str],
        log_date: date,
        global_index: int,
    ) -> tuple[str, int] | None:
        """Resolve a global 1-based display index to (session_id, local_1based_index).

        The global ordering matches the flat merged list sorted by timestamp —
        the same order used by get_logs_by_date_multi() and displayed by the
        daily report.  Returns None when *global_index* is out of range.

        Note: this is a read-only helper.  For atomic resolve+delete use
        ``delete_log_by_global_index`` instead.
        """
        indexed: list[tuple[str, str, int]] = []  # (timestamp, session_id, local_idx)
        for session in sessions:
            monthly = self._load_monthly(session, log_date.year, log_date.month)
            date_key = log_date.isoformat()
            entries = monthly.get(date_key, [])
            for local_idx, entry in enumerate(entries, start=1):
                indexed.append((entry.get("timestamp", ""), session, local_idx))
        indexed.sort(key=lambda x: x[0])
        if global_index < 1 or global_index > len(indexed):
            return None
        _, session_id, local_idx = indexed[global_index - 1]
        return session_id, local_idx

    async def delete_log_by_global_index(
        self,
        sessions: list[str],
        log_date: date,
        global_index: int,
    ) -> bool:
        """Resolve a global 1-based index and delete the entry atomically.

        Acquiring the write lock for the entire resolve-then-delete sequence
        prevents TOCTOU races in concurrent async scenarios.
        Returns True on success, False when the index is out of range.
        """
        if global_index < 1:
            return False
        async with self._write_lock:
            # Rebuild the indexed list under the lock to get a consistent view.
            indexed: list[tuple[str, str, int]] = []
            for session in sessions:
                monthly = self._load_monthly(session, log_date.year, log_date.month)
                date_key = log_date.isoformat()
                entries = monthly.get(date_key, [])
                for local_idx, entry in enumerate(entries, start=1):
                    indexed.append((entry.get("timestamp", ""), session, local_idx))
            indexed.sort(key=lambda x: x[0])
            if global_index > len(indexed):
                return False
            _, session_id, local_idx = indexed[global_index - 1]

            # Delete the resolved entry (re-reads under the same lock).
            monthly = self._load_monthly(session_id, log_date.year, log_date.month)
            date_key = log_date.isoformat()
            entries = monthly.get(date_key, [])
            if local_idx < 1 or local_idx > len(entries):
                return False
            entries.pop(local_idx - 1)
            if entries:
                monthly[date_key] = entries
            else:
                del monthly[date_key]
            self._save_monthly(session_id, log_date.year, log_date.month, monthly)
            return True
