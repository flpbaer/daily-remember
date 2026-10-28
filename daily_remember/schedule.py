"""Tipos de agendamento e cálculo do próximo envio.

Um agendamento é um dict salvo como JSON, por exemplo:
    {"type": "daily", "time": "09:00"}
    {"type": "weekdays", "time": "09:00"}
    {"type": "weekly", "days": ["mon", "wed"], "time": "09:00"}
    {"type": "monthly", "day": 5, "time": "09:00"}      (day pode ser "L" = último dia)
    {"type": "once", "at": "2026-10-23T09:00"}
    {"type": "cron", "expr": "30 9 * * mon-fri"}
    {"type": "times", "times": ["09:00", "14:00", "19:00"], "days": ["mon", ...]}
    {"type": "interval", "every": 60, "start": "09:00", "end": "18:00", "days": ["mon", ...]}
        (every em minutos; envia em start, start+every, ... até end inclusive)
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from croniter import croniter

from .config import TZ

Schedule = Dict[str, Any]

WEEKDAYS = [
    ("mon", "Segunda"), ("tue", "Terça"), ("wed", "Quarta"), ("thu", "Quinta"),
    ("fri", "Sexta"), ("sat", "Sábado"), ("sun", "Domingo"),
]
DAY_LABEL = dict(WEEKDAYS)
ALL_DAYS = [code for code, _ in WEEKDAYS]
WORKDAYS = ALL_DAYS[:5]
MULTI_PER_DAY = ("times", "interval")


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


def daily_times(spec: Schedule) -> List[Tuple[int, int]]:
    """Horários do dia (ordenados) dos tipos que disparam várias vezes ao dia."""
    if spec["type"] == "times":
        return sorted(parse_time(t) for t in spec["times"])
    start_h, start_m = parse_time(spec["start"])
    end_h, end_m = parse_time(spec["end"])
    start, end = start_h * 60 + start_m, end_h * 60 + end_m
    return [divmod(minute, 60) for minute in range(start, end + 1, spec["every"])]


def _next_multi(spec: Schedule, after_local: datetime) -> Optional[datetime]:
    times = daily_times(spec)
    day: date = after_local.date()
    for _ in range(8):  # uma semana + 1 dia cobre qualquer combinação de dias
        if ALL_DAYS[day.weekday()] in spec["days"]:
            for hour, minute in times:
                candidate = datetime.combine(day, time(hour, minute), tzinfo=TZ)
                if candidate > after_local:
                    return candidate
        day += timedelta(days=1)
    return None


def describe_days(days: List[str]) -> str:
    if sorted(days, key=ALL_DAYS.index) == ALL_DAYS:
        return "todo dia"
    if sorted(days, key=ALL_DAYS.index) == WORKDAYS:
        return "dias úteis"
    return ", ".join(DAY_LABEL[d] for d in sorted(days, key=ALL_DAYS.index))


def describe_every(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if not mins:
        return "1 hora" if hours == 1 else f"{hours} horas"
    if not hours:
        return f"{mins} min"
    return f"{hours}h{mins:02d}"


def to_cron(spec: Schedule) -> Optional[str]:
    kind = spec["type"]
    if kind == "cron":
        return spec["expr"]
    if kind in ("once",) + MULTI_PER_DAY:
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
    if spec["type"] in MULTI_PER_DAY:
        return _next_multi(spec, after_local)
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
    if kind == "times":
        times = ", ".join(f"{h:02d}:{m:02d}" for h, m in daily_times(spec))
        return f"{describe_days(spec['days']).capitalize()} às {times}"
    if kind == "interval":
        return (f"A cada {describe_every(spec['every'])}, das {spec['start']} às {spec['end']}, "
                f"{describe_days(spec['days'])}")
    return kind
