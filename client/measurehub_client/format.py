"""Texts and colours of statuses (the UI is in Russian)."""

from __future__ import annotations

from datetime import datetime

JOB_STATUS = {
    "queued": "В очереди",
    "running": "Измеряется",
    "processing": "Обработка",
    "cancelling": "Отменяется",
    "cancelled": "Отменено",
    "completed": "Завершено",
    "failed": "Ошибка",
}
FINISHED = frozenset({"cancelled", "completed", "failed"})
CANCELLABLE = frozenset({"queued", "running"})

INSTRUMENT_STATUS = {
    "online": "В сети",
    "offline": "Не в сети",
    "busy": "Занят",
    "error": "Ошибка",
}

COLORS = {
    "queued": "#7f8c8d",
    "running": "#2980b9",
    "processing": "#8e44ad",
    "cancelling": "#d35400",
    "cancelled": "#7f8c8d",
    "completed": "#27ae60",
    "failed": "#c0392b",
    "online": "#27ae60",
    "offline": "#7f8c8d",
    "busy": "#2980b9",
    "error": "#c0392b",
}

LOG_COLORS = {"info": "#2c3e50", "warning": "#d35400", "error": "#c0392b"}

ERROR_TEXT = {
    "instrument_connection": "нет связи с прибором",
    "instrument_timeout": "прибор не отвечает",
    "instrument_error": "ошибка прибора",
    "worker_lost": "потерян воркер",
    "lock_lost": "потеряна блокировка прибора",
    "dispatch_failed": "очередь заданий недоступна",
    "internal_error": "внутренняя ошибка",
}


def job_status_text(status: str) -> str:
    return JOB_STATUS.get(status, status)


def instrument_status_text(status: str) -> str:
    return INSTRUMENT_STATUS.get(status, status)


def progress_percent(points_done: int, points_total: int) -> int:
    return round(100 * points_done / points_total) if points_total else 0


def local_time(iso: str | None) -> str:
    """ISO timestamp from the server (UTC) as local ``YYYY-MM-DD HH:MM:SS``."""
    if not iso:
        return "—"
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def error_summary(code: str | None, message: str | None) -> str:
    if not code:
        return ""
    text = ERROR_TEXT.get(code, code)
    return f"{text}: {message}" if message else text
