"""Montagem do texto do lembrete (usado pelo bot e pela pré-visualização da CLI)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

WEEKDAY_NAMES = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
                 "sexta-feira", "sábado", "domingo"]

VARIABLES_HELP = "{nome}, {data}, {dia_semana}, {hora}"

DELIVERY_LABEL = {
    "dm": "Privado (DM para cada pessoa)",
    "channel": "Canal (anúncio geral)",
    "both": "Canal + privado",
}


def apply_variables(text: str, when: datetime, name: str = "pessoal") -> str:
    """{nome} vira o nome da pessoa no privado e "pessoal" no canal."""
    return (text
            .replace("{nome}", name)
            .replace("{data}", when.strftime("%d/%m/%Y"))
            .replace("{dia_semana}", WEEKDAY_NAMES[when.weekday()])
            .replace("{hora}", when.strftime("%H:%M")))


def mention_tokens(mentions: List[Dict[str, Any]]) -> str:
    tokens = []
    for m in mentions:
        if m["type"] == "everyone":
            tokens.append("@everyone")
        elif m["type"] == "here":
            tokens.append("@here")
        elif m["type"] == "role":
            tokens.append(f"<@&{m['id']}>")
        elif m["type"] == "user":
            tokens.append(f"<@{m['id']}>")
    return " ".join(tokens)


def mention_label(m: Dict[str, Any]) -> str:
    if m["type"] in ("everyone", "here"):
        return f"@{m['type']}"
    name = m.get("name") or m["id"]
    return f"@{name}" + (" (cargo)" if m["type"] == "role" else "")
