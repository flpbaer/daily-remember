"""Tipos de agendamento e cálculo do próximo envio.

Um agendamento é um dict salvo como JSON, por exemplo:
    {"type": "daily", "time": "09:00"}
    {"type": "weekdays", "time": "09:00"}
    {"type": "weekly", "days": ["mon", "wed"], "time": "09:00"}
    {"type": "monthly", "day": 5, "time": "09:00"}      (day pode ser "L" = último dia)
    {"type": "once", "at": "2026-10-23T09:00"}
    {"type": "cron", "expr": "30 9 * * mon-fri"}
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from croniter import croniter

from .config import TZ

Schedule = Dict[str, Any]

WEEKDAYS = [
    ("mon", "Segunda"), ("tue", "Terça"), ("wed", "Quarta"), ("thu", "Quinta"),
    ("fri", "Sexta"), ("sat", "Sábado"), ("sun", "Domingo"),
]
DAY_LABEL = dict(WEEKDAYS)


def parse_time(value: str) -> Tuple[int, int]:
    hour, minute = (int(p) for p in value.strip().split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(value)
    return hour, minute


def is_valid_time(value: str) -> bool:
    try:
        parse_time(value)
        return True
    except ValueError:
        return False


def to_cron(spec: Schedule) -> Optional[str]:
    kind = spec["type"]
    if kind == "cron":
        return spec["expr"]
    if kind == "once":
        return None
    hour, minute = parse_time(spec["time"])
    if kind == "daily":
        return f"{minute} {hour} * * *"
    if kind == "weekdays":
        return f"{minute} {hour} * * mon-fri"
    if kind == "weekly":
        return f"{minute} {hour} * * {','.join(spec['days'])}"
    if kind == "monthly":
        return f"{minute} {hour} {spec['day']} * *"
    raise ValueError(f"Tipo de agendamento desconhecido: {kind}")


def next_run(spec: Schedule, after: Optional[datetime] = None) -> Optional[datetime]:
    """Próximo envio estritamente depois de `after` (None se não houver mais)."""
    after_local = (after or datetime.now(timezone.utc)).astimezone(TZ)
    if spec["type"] == "once":
        at = datetime.fromisoformat(spec["at"]).replace(tzinfo=TZ)
        return at if at > after_local else None
    return croniter(to_cron(spec), after_local).get_next(datetime)


def upcoming(spec: Schedule, count: int = 3) -> List[datetime]:
    runs: List[datetime] = []
    current = next_run(spec)
    while current and len(runs) < count:
        runs.append(current)
        current = next_run(spec, current)
    return runs


def describe(spec: Schedule) -> str:
    kind = spec["type"]
    if kind == "daily":
        return f"Todo dia às {spec['time']}"
    if kind == "weekdays":
        return f"Dias úteis (seg–sex) às {spec['time']}"
    if kind == "weekly":
        days = ", ".join(DAY_LABEL[d] for d in spec["days"])
        return f"{days} às {spec['time']}"
    if kind == "monthly":
        day = "último dia" if spec["day"] == "L" else f"dia {spec['day']}"
        return f"Todo mês, {day}, às {spec['time']}"
    if kind == "once":
        at = datetime.fromisoformat(spec["at"])
        return f"Uma vez: {at:%d/%m/%Y às %H:%M}"
    if kind == "cron":
        return f"Cron: {spec['expr']}"
    return kind
