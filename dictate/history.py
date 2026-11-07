"""История сессий диктовки: ``sessions.json`` (версия 0.2.0).

Хранит результаты прошлых голосовых вводов: время, длительность, модель,
исходный/исправленный текст. Только stdlib: без Tk, без сети.

Файл по умолчанию — ``config.data_dir()/sessions.json`` (в портативной версии
это папка ``data`` рядом с программой). Запись атомарная: сначала ``.tmp``,
затем ``os.replace``. Битый JSON читается как пустая история.

Старый ``history.json`` (``[{"text", "ts"}, ...]``) импортируется при первом
обращении, если ``sessions.json`` ещё нет; сам старый файл не удаляется.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LIMIT = 300
SESSION_ID_BYTES = 6  # 12 hex-символов
SOURCE_DICTATION = "dictation"
SOURCE_IMPORT = "import"


# --------------------------------------------------------------------------- paths


def _data_dir() -> Path:
    try:
        from . import config

        return config.data_dir()
    except Exception:  # noqa: BLE001 - история не должна падать из-за конфига
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "WinDictate"
        return Path.home() / "AppData" / "Local" / "WinDictate"


def default_sessions_path() -> Path:
    return _data_dir() / "sessions.json"


# --------------------------------------------------------------------------- helpers


def _iso_local(ts: float) -> str:
    """ISO 8601 локального времени с часовым поясом (``2026-11-07T20:14:05+03:00``).

    Windows иногда не умеет локальное время для очень старых timestamp (OSError) —
    тогда отдаём UTC, чтобы импорт старой истории не падал.
    """
    try:
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _new_id(existing: set[str]) -> str:
    while True:
        session_id = secrets.token_hex(SESSION_ID_BYTES)
        if session_id not in existing:
            return session_id


def _copy(record: dict[str, Any]) -> dict[str, Any]:
    return dict(record)


class SessionStore:
    """Потокобезопасное JSON-хранилище сессий диктовки."""

    def __init__(self, path: Path | None = None, limit: int = DEFAULT_LIMIT) -> None:
        self.path = Path(path) if path is not None else default_sessions_path()
        self.limit = max(1, _as_int(limit, DEFAULT_LIMIT))
        self._lock = threading.RLock()
        self._sessions: list[dict[str, Any]] = []
        self._loaded = False

    # -- persistence -------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._sessions = self._read_sessions()
        if self._sessions is None:  # файла нет — пробуем старый history.json
            self._sessions = self._read_legacy()
            if self._sessions:
                self._trim()
                self._write(self._sessions)

    def _read_sessions(self) -> list[dict[str, Any]] | None:
        if not self.path.exists():
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return []  # битый файл = пустая история
        if not isinstance(raw, list):
            return []
        existing: set[str] = set()
        sessions: list[dict[str, Any]] = []
        for item in raw:
            record = self._normalize_record(item, existing)
            if record is not None:
                sessions.append(record)
        sessions.sort(key=lambda record: record["ts"], reverse=True)
        return sessions

    def _read_legacy(self) -> list[dict[str, Any]]:
        """Импорт старого ``history.json`` из той же папки, что и ``sessions.json``.

        Для пути по умолчанию это ровно ``config.data_dir()/history.json``; явный
        путь (тесты, перенос данных) ищет старый файл только рядом с собой.
        """
        imported = self._read_legacy_file(self.path.with_name("history.json"))
        imported.sort(key=lambda record: record["ts"], reverse=True)
        return imported

    def _read_legacy_file(self, legacy: Path) -> list[dict[str, Any]]:
        if not legacy.exists():
            return []
        try:
            with open(legacy, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        existing: set[str] = set()
        imported: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            ts = _as_float(item.get("ts"), time.time())
            imported.append(
                self._make_record(
                    text,
                    ts=ts,
                    duration_ms=0,
                    model="",
                    source=SOURCE_IMPORT,
                    existing=existing,
                )
            )
        return imported

    def _write(self, sessions: list[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(sessions, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            # История — вспомогательная функция: ошибка записи не должна ронять диктовку.
            pass

    def _save(self) -> None:
        self._write(self._sessions)

    # -- records -----------------------------------------------------------
    @staticmethod
    def _make_record(
        text: str,
        *,
        ts: float,
        duration_ms: int,
        model: str,
        source: str,
        existing: set[str],
        session_id: str | None = None,
        edited: bool = False,
    ) -> dict[str, Any]:
        text = str(text or "")
        session_id = session_id or _new_id(existing)
        existing.add(session_id)
        return {
            "id": session_id,
            "created_at": _iso_local(ts),
            "ts": round(float(ts), 3),
            "duration_ms": max(0, _as_int(duration_ms, 0)),
            "chars": len(text),
            "text": text,
            "model": str(model or ""),
            "source": str(source or SOURCE_DICTATION),
            "edited": bool(edited),
        }

    @classmethod
    def _normalize_record(cls, item: Any, existing: set[str]) -> dict[str, Any] | None:
        """Починить запись из файла: недостающие поля — дефолты, битые — отбросить."""
        if not isinstance(item, dict):
            return None
        text = item.get("text")
        if not isinstance(text, str):
            return None
        session_id = item.get("id")
        if not isinstance(session_id, str) or not session_id.strip():
            session_id = None
        elif session_id in existing:
            session_id = None
        ts = _as_float(item.get("ts"), 0.0)
        if ts <= 0:
            created_at = item.get("created_at")
            try:
                ts = datetime.fromisoformat(str(created_at)).timestamp()
            except (TypeError, ValueError):
                ts = time.time()
        record = cls._make_record(
            text,
            ts=ts,
            duration_ms=_as_int(item.get("duration_ms"), 0),
            model=str(item.get("model") or ""),
            source=str(item.get("source") or SOURCE_DICTATION),
            existing=existing,
            session_id=session_id,
            edited=bool(item.get("edited", False)),
        )
        record["chars"] = len(text)
        if isinstance(item.get("created_at"), str) and item["created_at"]:
            record["created_at"] = item["created_at"]
        return record

    def _trim(self) -> None:
        if len(self._sessions) > self.limit:
            del self._sessions[self.limit:]

    # -- public API --------------------------------------------------------
    def add(
        self,
        text: str,
        *,
        duration_ms: int = 0,
        model: str = "",
        source: str = SOURCE_DICTATION,
    ) -> dict[str, Any]:
        """Сохранить новую сессию (новые первыми) и вернуть её запись."""
        with self._lock:
            self._ensure_loaded()
            ts = time.time()
            existing = {record["id"] for record in self._sessions}
            record = self._make_record(
                text,
                ts=ts,
                duration_ms=duration_ms,
                model=model,
                source=source,
                existing=existing,
            )
            self._sessions.insert(0, record)
            self._trim()
            self._save()
            return _copy(record)

    def list(self, query: str = "") -> list[dict[str, Any]]:
        """Сессии новыми первыми; ``query`` — регистронезависимый поиск по тексту."""
        with self._lock:
            self._ensure_loaded()
            needle = str(query or "").strip().lower()
            sessions = sorted(self._sessions, key=lambda record: record["ts"], reverse=True)
            if needle:
                sessions = [record for record in sessions if needle in str(record.get("text", "")).lower()]
            return [_copy(record) for record in sessions]

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_loaded()
            for record in self._sessions:
                if record["id"] == session_id:
                    return _copy(record)
            return None

    def update_text(self, session_id: str, text: str) -> bool:
        """Обновить текст сессии: ``edited=True``, новые ``chars``/``ts``."""
        with self._lock:
            self._ensure_loaded()
            for record in self._sessions:
                if record["id"] != session_id:
                    continue
                value = str(text or "")
                record["text"] = value
                record["chars"] = len(value)
                record["edited"] = True
                record["ts"] = round(time.time(), 3)
                self._save()
                return True
            return False

    def delete(self, session_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            for index, record in enumerate(self._sessions):
                if record["id"] == session_id:
                    del self._sessions[index]
                    self._save()
                    return True
            return False

    def clear(self) -> int:
        """Удалить всю историю и вернуть число удалённых записей."""
        with self._lock:
            self._ensure_loaded()
            removed = len(self._sessions)
            self._sessions = []
            self._save()
            return removed

    def count(self) -> int:
        with self._lock:
            self._ensure_loaded()
            return len(self._sessions)
