"""CLI para gerenciar os lembretes.

Sem argumentos abre um menu interativo; também aceita comandos diretos
(ex: `./dr add`, `./dr list`, `./dr pause 3`). Tudo é gravado no banco e o
bot, se estiver rodando, aplica as mudanças em poucos segundos.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import questionary
from croniter import croniter
from questionary import Choice, Separator
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import db, schedule
from .config import POLL_SECONDS, TZ
from .message import DELIVERY_LABEL, VARIABLES_HELP, mention_label

console = Console()

MANUAL = "__manual__"


# --- Formatação ------------------------------------------------------------

def fmt_dt(value: Optional[str]) -> str:
    dt = db.from_iso(value)
    return dt.astimezone(TZ).strftime("%d/%m/%Y %H:%M") if dt else "—"


def status_of(r: Dict[str, Any]) -> str:
    if r["active"]:
        return "[green]ativo[/]"
    if r["schedule"]["type"] == "once" and r["last_run_at"]:
        return "[blue]concluído[/]"
    return "[yellow]pausado[/]"


def channel_name(channel_id: str) -> str:
    for c in db.list_cache("channel"):
        if c["id"] == channel_id:
            return f"#{c['name']}"
    return channel_id


def destination_of(r: Dict[str, Any]) -> str:
    parts = []
    if r["delivery"] in ("channel", "both"):
        parts.append(channel_name(r["channel_id"]))
    if r["delivery"] in ("dm", "both"):
        parts.append("DM: " + ", ".join(mention_label(p) for p in r["recipients"]))
    return " + ".join(parts)


def print_reminder(data: Dict[str, Any], header: str = "Pré-visualização") -> None:
    lines = [f"[bold]Destino:[/] {destination_of(data)}",
             f"[bold]Agenda:[/] {schedule.describe(data['schedule'])}"]
    runs = schedule.upcoming(data["schedule"])
    if runs:
        lines.append("[bold]Próximos envios:[/] " + ", ".join(f"{r:%d/%m %H:%M}" for r in runs))
    if data["mentions"]:
        lines.append("[bold]Menções:[/] " + " ".join(mention_label(m) for m in data["mentions"]))
    lines.append("")
    if data.get("title"):
        lines.append(f"[bold underline]{data['title']}[/]")
    lines.append(data["content"])
    for link in data["links"]:
        lines.append(f"🔗 [link={link['url']}]{link['label']}[/link]  [dim]{link['url']}[/]")
    console.print(Panel("\n".join(lines), title=f"{header}: {data['name']}", expand=False))


# --- Perguntas do assistente ----------------------------------------------

def ask_channel(current: Optional[str]) -> str:
    channels = db.list_cache("channel")
    if channels:
        choices = [Choice(f"#{c['name']}  ({c['guild']})", value=c["id"]) for c in channels]
        choices.append(Choice("Digitar ID manualmente", value=MANUAL))
        default = current if any(c["id"] == current for c in channels) else None
        picked = questionary.select("Canal:", choices=choices, default=default).unsafe_ask()
        if picked != MANUAL:
            return picked
    else:
        console.print("[dim]Dica: com o bot rodando, os canais aparecem aqui como opções. "
                      "Sem isso, ative o Modo Desenvolvedor no Discord e use 'Copiar ID' no canal.[/]")
    return questionary.text(
        "ID do canal:", default=current or "",
        validate=lambda v: v.strip().isdigit() or "Informe apenas números (ID do canal)",
    ).unsafe_ask().strip()


def ask_people(question: str, current: List[Dict[str, Any]], broadcast: bool) -> List[Dict[str, Any]]:
    """Checkbox com cargos e membros do servidor (do cache do bot) + IDs manuais.

    `broadcast` inclui @everyone/@here, que só fazem sentido em canal.
    """
    selected_keys = {(m["type"], m.get("id")) for m in current}

    def choice(label: str, value: Dict[str, Any]) -> Choice:
        return Choice(label, value=value, checked=(value["type"], value.get("id")) in selected_keys)

    choices: List[Any] = []
    if broadcast:
        choices += [choice("@everyone (todos do servidor)", {"type": "everyone"}),
                    choice("@here (só quem está online)", {"type": "here"})]
    known = set()
    members = db.list_cache("member")
    if members:
        choices.append(Separator("— Membros —"))
        for m in members:
            known.add(("user", m["id"]))
            choices.append(choice(m["name"], {"type": "user", "id": m["id"], "name": m["name"]}))
    roles = db.list_cache("role")
    if roles:
        choices.append(Separator("— Cargos (todos que têm o cargo) —"))
        for r in roles:
            known.add(("role", r["id"]))
            choices.append(choice(f"@{r['name']}", {"type": "role", "id": r["id"], "name": r["name"]}))
    # Pessoas/cargos adicionados por ID antes continuam aparecendo na lista
    extras = [m for m in current if m["type"] in ("role", "user") and (m["type"], m["id"]) not in known]
    if extras:
        choices.append(Separator("— Adicionados por ID —"))
        choices.extend(choice(mention_label(m), m) for m in extras)

    picked: List[Dict[str, Any]] = []
    if choices:
        picked = questionary.checkbox(question, choices=choices,
                                      instruction="(espaço marca, enter confirma)").unsafe_ask()
    else:
        console.print("[dim]Inicie o bot (./dr bot) para os membros e cargos aparecerem aqui. "
                      "Por enquanto, informe os IDs.[/]")

    if not choices or questionary.confirm("Adicionar alguém por ID?", default=False).unsafe_ask():
        for kind, label in (("user", "usuários"), ("role", "cargos")):
            raw = questionary.text(f"IDs de {label}, separados por vírgula (vazio para pular):").unsafe_ask()
            for part in raw.replace(" ", "").split(","):
                if part.isdigit():
                    picked.append({"type": kind, "id": part, "name": part})
    return picked


def ask_recipients(current: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    while True:
        picked = ask_people("Quem recebe no privado?", current, broadcast=False)
        if picked:
            return picked
        console.print("[red]Escolha pelo menos uma pessoa ou cargo.[/]")


def ask_links(current: List[Dict[str, str]]) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    if current:
        for link in current:
            console.print(f"  🔗 {link['label']} → {link['url']}")
        if questionary.confirm("Manter esses links?", default=True).unsafe_ask():
            links = list(current)

    def valid_url(value: str):
        parsed = urlparse(value.strip())
        return (parsed.scheme in ("http", "https") and bool(parsed.netloc)) or \
            "URL inválida (precisa começar com http:// ou https://)"

    while len(links) < 25:
        prompt = "Adicionar outro link?" if links else "Adicionar um link (vira um botão na mensagem)?"
        if not questionary.confirm(prompt, default=False).unsafe_ask():
            break
        url = questionary.text("URL:", validate=valid_url).unsafe_ask().strip()
        label = questionary.text("Texto do botão:", default=urlparse(url).netloc).unsafe_ask().strip()
        links.append({"label": label or url, "url": url})
    return links


def ask_schedule(current: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    current = current or {}
    kind = questionary.select(
        "Quando enviar?",
        choices=[
            Choice("Todos os dias", "daily"),
            Choice("Dias úteis (segunda a sexta)", "weekdays"),
            Choice("Dias específicos da semana", "weekly"),
            Choice("Uma vez por mês", "monthly"),
            Choice("Uma única vez (data e hora)", "once"),
            Choice("Avançado: expressão cron", "cron"),
        ],
        default=current.get("type", "weekdays"),
    ).unsafe_ask()

    def ask_time() -> str:
        value = questionary.text(
            "Horário (HH:MM):", default=current.get("time", "09:00"),
            validate=lambda v: schedule.is_valid_time(v) or "Use o formato HH:MM, ex: 09:30",
        ).unsafe_ask()
        hour, minute = schedule.parse_time(value)
        return f"{hour:02d}:{minute:02d}"

    if kind in ("daily", "weekdays"):
        return {"type": kind, "time": ask_time()}

    if kind == "weekly":
        chosen = set(current.get("days", []))
        days = questionary.checkbox(
            "Quais dias?",
            choices=[Choice(label, value=code, checked=code in chosen) for code, label in schedule.WEEKDAYS],
            validate=lambda v: bool(v) or "Escolha pelo menos um dia",
        ).unsafe_ask()
        return {"type": "weekly", "days": days, "time": ask_time()}

    if kind == "monthly":
        def valid_day(v: str):
            v = v.strip().upper()
            return v == "L" or (v.isdigit() and 1 <= int(v) <= 31) or "Informe 1 a 31, ou L"
        day = questionary.text(
            "Dia do mês (1-31, ou L para o último dia):", default=str(current.get("day", 1)),
            validate=valid_day, instruction="(meses sem o dia escolhido são pulados)",
        ).unsafe_ask().strip().upper()
        return {"type": "monthly", "day": day if day == "L" else int(day), "time": ask_time()}

    if kind == "once":
        default_at = datetime.fromisoformat(current["at"]) if current.get("at") else None

        def parse(date_str: str, time_str: str) -> datetime:
            hour, minute = schedule.parse_time(time_str)
            return datetime.strptime(date_str.strip(), "%d/%m/%Y").replace(hour=hour, minute=minute)

        while True:
            date_str = questionary.text(
                "Data (dd/mm/aaaa):",
                default=default_at.strftime("%d/%m/%Y") if default_at else datetime.now(TZ).strftime("%d/%m/%Y"),
            ).unsafe_ask()
            time_str = questionary.text(
                "Horário (HH:MM):", default=default_at.strftime("%H:%M") if default_at else "09:00",
            ).unsafe_ask()
            try:
                at = parse(date_str, time_str)
            except ValueError:
                console.print("[red]Data ou horário inválido.[/]")
                continue
            if at.replace(tzinfo=TZ) <= datetime.now(TZ):
                console.print("[red]Essa data/hora já passou.[/]")
                continue
            return {"type": "once", "at": at.strftime("%Y-%m-%dT%H:%M")}

    console.print("[dim]Formato: minuto hora dia-do-mês mês dia-da-semana. "
                  "Ex: '30 9 * * mon-fri' = 9h30 em dias úteis; '0 18 * * fri' = sexta 18h.[/]")
    expr = questionary.text(
        "Expressão cron:", default=current.get("expr", ""),
        validate=lambda v: croniter.is_valid(v.strip()) or "Expressão cron inválida",
    ).unsafe_ask().strip()
    return {"type": "cron", "expr": expr}


def ask_reminder(existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    existing = existing or {}
    name = questionary.text(
        "Nome do lembrete (só para você identificar):", default=existing.get("name", ""),
        validate=lambda v: bool(v.strip()) or "Dê um nome",
    ).unsafe_ask().strip()
    delivery = questionary.select(
        "Para onde enviar?",
        choices=[Choice(label, value) for value, label in DELIVERY_LABEL.items()],
        default=existing.get("delivery", "dm"),
    ).unsafe_ask()
    channel_id, mentions, recipients = "", [], []
    if delivery in ("dm", "both"):
        recipients = ask_recipients(existing.get("recipients", []))
    if delivery in ("channel", "both"):
        channel_id = ask_channel(existing.get("channel_id"))
        mentions = ask_people("Quem mencionar no canal?", existing.get("mentions", []), broadcast=True)
    title = questionary.text(
        "Título (opcional, deixa a mensagem num card destacado):", default=existing.get("title") or "",
    ).unsafe_ask().strip()
    content = questionary.text(
        "Mensagem:", default=existing.get("content", ""), multiline=True,
        instruction=f"\n  (Esc e depois Enter para concluir. Variáveis: {VARIABLES_HELP})\n",
        validate=lambda v: (bool(v.strip()) and len(v) <= 1800) or "Mensagem vazia ou acima de 1800 caracteres",
    ).unsafe_ask().strip()
    links = ask_links(existing.get("links", []))
    spec = ask_schedule(existing.get("schedule"))
    return {"name": name, "channel_id": channel_id, "title": title or None, "content": content,
            "mentions": mentions, "links": links, "schedule": spec,
            "delivery": delivery, "recipients": recipients}


# --- Comandos --------------------------------------------------------------

def pick_reminder(reminder_id: Optional[int], action: str) -> Optional[Dict[str, Any]]:
    if reminder_id is not None:
        reminder = db.get_reminder(reminder_id)
        if not reminder:
            console.print(f"[red]Lembrete #{reminder_id} não encontrado.[/]")
        return reminder
    reminders = db.list_reminders()
    if not reminders:
        console.print("Nenhum lembrete cadastrado ainda.")
        return None
    choices = [Choice(f"#{r['id']} {r['name']} — {schedule.describe(r['schedule'])}", value=r["id"])
               for r in reminders]
    return db.get_reminder(questionary.select(f"Qual lembrete {action}?", choices=choices).unsafe_ask())


def cmd_add(_args: argparse.Namespace) -> None:
    data = ask_reminder()
    print_reminder(data)
    if not questionary.confirm("Salvar?", default=True).unsafe_ask():
        console.print("Descartado.")
        return
    next_at = schedule.next_run(data["schedule"])
    reminder_id = db.create_reminder(data, next_at)
    console.print(f"[green]✔ Lembrete #{reminder_id} criado.[/] Próximo envio: {fmt_dt(db.to_iso(next_at))}")
    if questionary.confirm("Enviar um teste agora para conferir como ficou?", default=False).unsafe_ask():
        db.request_send_now(reminder_id)
        console.print(f"Teste agendado; o bot envia em até {POLL_SECONDS}s.")


def cmd_list(_args: argparse.Namespace) -> None:
    reminders = db.list_reminders()
    if not reminders:
        console.print("Nenhum lembrete cadastrado. Use [bold]./dr add[/] para criar o primeiro.")
        return
    table = Table(title="Lembretes")
    for col in ("ID", "Nome", "Status", "Agenda", "Destino", "Próximo envio", "Último envio"):
        table.add_column(col)
    for r in reminders:
        table.add_row(str(r["id"]), r["name"], status_of(r), schedule.describe(r["schedule"]),
                      destination_of(r), fmt_dt(r["next_run_at"]), fmt_dt(r["last_run_at"]))
    console.print(table)


def cmd_show(args: argparse.Namespace) -> None:
    reminder = pick_reminder(args.id, "ver")
    if reminder:
        print_reminder(reminder, header=f"#{reminder['id']}")


def cmd_edit(args: argparse.Namespace) -> None:
    reminder = pick_reminder(args.id, "editar")
    if not reminder:
        return
    data = ask_reminder(reminder)
    print_reminder(data)
    if not questionary.confirm("Salvar alterações?", default=True).unsafe_ask():
        console.print("Nada foi alterado.")
        return
    active = bool(reminder["active"]) or questionary.confirm(
        "Este lembrete está inativo. Reativar?", default=True).unsafe_ask()
    next_at = schedule.next_run(data["schedule"])
    db.update_reminder(reminder["id"], data, next_at, active)
    console.print(f"[green]✔ Lembrete #{reminder['id']} atualizado.[/]")


def cmd_pause(args: argparse.Namespace) -> None:
    reminder = pick_reminder(args.id, "pausar")
    if reminder:
        db.set_active(reminder["id"], False, None)
        console.print(f"[yellow]⏸ Lembrete #{reminder['id']} pausado.[/]")


def cmd_resume(args: argparse.Namespace) -> None:
    reminder = pick_reminder(args.id, "retomar")
    if not reminder:
        return
    next_at = schedule.next_run(reminder["schedule"])
    if not next_at:
        console.print("[red]Esse lembrete não tem mais datas futuras. Edite a agenda com ./dr edit.[/]")
        return
    db.set_active(reminder["id"], True, next_at)
    console.print(f"[green]▶ Lembrete #{reminder['id']} ativo.[/] Próximo envio: "
                  f"{next_at.astimezone(TZ):%d/%m/%Y %H:%M}")


def cmd_delete(args: argparse.Namespace) -> None:
    reminder = pick_reminder(args.id, "excluir")
    if reminder and questionary.confirm(f"Excluir '{reminder['name']}' de vez?", default=False).unsafe_ask():
        db.delete_reminder(reminder["id"])
        console.print(f"🗑 Lembrete #{reminder['id']} excluído.")


def cmd_send(args: argparse.Namespace) -> None:
    reminder = pick_reminder(args.id, "enviar agora")
    if reminder:
        db.request_send_now(reminder["id"])
        console.print(f"📨 O bot vai enviar '{reminder['name']}' em até {POLL_SECONDS}s "
                      "(sem alterar a agenda). O bot precisa estar rodando.")


def cmd_history(args: argparse.Namespace) -> None:
    rows = db.history(args.id, args.n)
    if not rows:
        console.print("Nenhum envio registrado ainda.")
        return
    table = Table(title="Histórico de envios")
    for col in ("Quando", "Lembrete", "Status", "Detalhe"):
        table.add_column(col)
    colors = {"enviado": "green", "parcial": "yellow", "erro": "red", "pulado": "yellow"}
    for row in rows:
        color = colors.get(row["status"], "white")
        table.add_row(fmt_dt(row["sent_at"]), f"#{row['reminder_id']} {row['name'] or '(excluído)'}",
                      f"[{color}]{row['status']}[/]", row["detail"] or "")
    console.print(table)


def cmd_bot(_args: argparse.Namespace) -> None:
    from .bot import run
    run()


MENU = [
    ("Listar lembretes", "list"),
    ("Criar lembrete", "add"),
    ("Editar lembrete", "edit"),
    ("Ver detalhes / pré-visualizar", "show"),
    ("Pausar lembrete", "pause"),
    ("Retomar lembrete", "resume"),
    ("Enviar agora (teste)", "send"),
    ("Excluir lembrete", "delete"),
    ("Histórico de envios", "history"),
]


def interactive_menu() -> None:
    while True:
        console.print()
        action = questionary.select(
            "daily-remember — o que deseja fazer?",
            choices=[Choice(label, value) for label, value in MENU] + [Choice("Sair", "quit")],
        ).unsafe_ask()
        if action == "quit":
            return
        try:
            COMMANDS[action](argparse.Namespace(id=None, n=20))
        except KeyboardInterrupt:
            console.print("[dim]Cancelado.[/]")


COMMANDS = {
    "add": cmd_add, "list": cmd_list, "show": cmd_show, "edit": cmd_edit, "pause": cmd_pause,
    "resume": cmd_resume, "delete": cmd_delete, "send": cmd_send, "history": cmd_history, "bot": cmd_bot,
}


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="./dr", description="daily-remember: lembretes agendados no Discord. Sem comando, abre o menu.")
    sub = parser.add_subparsers(dest="cmd", metavar="comando")
    sub.add_parser("bot", help="inicia o bot (deixe rodando)")
    sub.add_parser("add", help="cria um lembrete")
    sub.add_parser("list", help="lista os lembretes")
    for name, help_text in (("show", "mostra/pré-visualiza um lembrete"), ("edit", "edita um lembrete"),
                            ("pause", "pausa um lembrete"), ("resume", "reativa um lembrete"),
                            ("delete", "exclui um lembrete"), ("send", "envia agora, sem mexer na agenda")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("id", type=int, nargs="?", help="ID do lembrete (se omitido, pergunta)")
    h = sub.add_parser("history", help="histórico de envios")
    h.add_argument("id", type=int, nargs="?")
    h.add_argument("-n", type=int, default=20, help="quantidade de registros")

    args = parser.parse_args(argv)
    try:
        if args.cmd is None:
            interactive_menu()
        else:
            COMMANDS[args.cmd](args)
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelado.[/]")
        sys.exit(130)
