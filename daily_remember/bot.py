"""Processo do bot: fica rodando e envia os lembretes na hora certa.

Também atende o /code-review (ver code_review.py).

Não guarda agendamentos em memória: a cada POLL_SECONDS consulta o banco, então
qualquer mudança feita pela CLI vale no próximo ciclo, sem reiniciar.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import tasks

from . import code_review, db, schedule
from .config import GRACE_MINUTES, MEMBERS_INTENT, POLL_SECONDS, TOKEN, TZ
from .message import apply_variables, mention_tokens

log = logging.getLogger("daily_remember")


class LinksView(discord.ui.View):
    def __init__(self, links: List[Dict[str, str]]):
        super().__init__(timeout=None)
        for link in links[:25]:
            self.add_item(discord.ui.Button(label=link["label"][:80], url=link["url"]))


class ReminderBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = MEMBERS_INTENT
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(code_review.code_review_command)
        self._commands_synced = False

    async def setup_hook(self) -> None:
        # Botões dos code reviews continuam respondendo mesmo em mensagens antigas
        self.add_dynamic_items(code_review.StatusButton, code_review.ResubmitButton,
                               code_review.NewReviewButton)
        self.dispatch_loop.start()
        self.sync_cache_loop.start()

    async def on_ready(self) -> None:
        log.info("Conectado como %s em %d servidor(es)", self.user, len(self.guilds))
        if not self._commands_synced:
            for guild in self.guilds:
                await self.sync_commands(guild)
            self._commands_synced = True

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.sync_commands(guild)

    async def sync_commands(self, guild: discord.Guild) -> None:
        """Registra os slash commands por servidor (aparecem na hora, ao contrário dos globais)."""
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Comandos registrados em %s", guild.name)
        except Exception as exc:
            log.error("Não consegui registrar os comandos em %s: %s", guild.name, exc)

    # --- Envio -------------------------------------------------------------

    @tasks.loop(seconds=POLL_SECONDS)
    async def dispatch_loop(self) -> None:
        now = datetime.now(timezone.utc)
        grace = timedelta(minutes=GRACE_MINUTES)
        for reminder in db.due_reminders(now):
            try:
                await self._dispatch(reminder, now, grace)
            except Exception:
                log.exception("Erro processando lembrete #%s", reminder["id"])
        try:
            await code_review.ensure_panel(self)
        except Exception:
            log.exception("Erro ao publicar o painel de code review")

    async def _dispatch(self, reminder: Dict[str, Any], now: datetime, grace: timedelta) -> None:
        scheduled_at = db.from_iso(reminder["next_run_at"])
        is_due = bool(reminder["active"] and scheduled_at and scheduled_at <= now)
        too_late = is_due and now - scheduled_at > grace

        if reminder["test_to"]:
            await self.send_test(reminder, int(reminder["test_to"]))

        sent_at = None
        if reminder["send_now"] or (is_due and not too_late):
            if await self.send_reminder(reminder):
                sent_at = now
        elif too_late:
            log.warning("Lembrete #%s pulado: horário %s passou há mais de %d min (bot estava fora?)",
                        reminder["id"], scheduled_at.astimezone(TZ), GRACE_MINUTES)
            db.log_send(reminder["id"], "pulado", f"atrasado desde {db.to_iso(scheduled_at)}")

        next_at = schedule.next_run(reminder["schedule"], now) if is_due else None
        db.finish_dispatch(reminder, sent_at, advance=is_due, next_run=next_at)

    async def send_reminder(self, reminder: Dict[str, Any]) -> bool:
        """Envia para o canal e/ou no privado. Retorna True se ao menos um envio deu certo."""
        delivery = reminder.get("delivery", "channel")
        ok, failures = 0, []

        if delivery in ("channel", "both"):
            try:
                channel_id = int(reminder["channel_id"])
                channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                await channel.send(**self.build_message(reminder))
                ok += 1
            except Exception as exc:
                failures.append(f"canal ({exc})")

        recipients = self.resolve_recipients(reminder["recipients"]) if delivery in ("dm", "both") else {}
        # O dono (./dr me) recebe todo envio no privado. Se ele não era destinatário,
        # a mensagem vem marcada como cópia para não confundir com um lembrete pessoal.
        copy_to: Optional[int] = None
        owner_id = db.get_setting("my_user_id")
        if owner_id and int(owner_id) not in recipients:
            copy_to = int(owner_id)
            recipients.update(self.resolve_recipients([{"type": "user", "id": owner_id, "name": "você"}]))

        for user_id, name in recipients.items():
            try:
                user = self.get_user(user_id) or await self.fetch_user(user_id)
                message = self.build_message(reminder, name=name, mentions=False)
                if user_id == copy_to:
                    note = f"📋 Cópia do lembrete #{reminder['id']} ({reminder['name']})"
                    message["content"] = f"{note}\n\n{message['content']}" if message.get("content") else note
                await user.send(**message)
                ok += 1
            except discord.Forbidden:
                failures.append(f"{name} (DM fechada)")
            except Exception as exc:
                failures.append(f"{name} ({exc})")

        total = ok + len(failures)
        status = "enviado" if not failures else ("parcial" if ok else "erro")
        detail = f"{ok}/{total} ok" + (f"; falhou: {', '.join(failures)}" if failures else "")
        if total == 0:
            status, detail = "erro", "nenhum destinatário encontrado"
        log.log(logging.INFO if status == "enviado" else logging.WARNING,
                "Lembrete #%s (%s): %s — %s", reminder["id"], reminder["name"], status, detail)
        db.log_send(reminder["id"], status, detail)
        return ok > 0

    async def send_test(self, reminder: Dict[str, Any], user_id: int) -> None:
        """Manda o lembrete só no privado de quem pediu o teste, sem mencionar ninguém."""
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            message = self.build_message(reminder, name=user.display_name, mentions=False)
            note = f"🧪 Teste do lembrete #{reminder['id']} ({reminder['name']})"
            message["content"] = f"{note}\n\n{message['content']}" if message.get("content") else note
            await user.send(**message)
        except Exception as exc:
            log.error("Falha no teste do lembrete #%s: %s", reminder["id"], exc)
            db.log_send(reminder["id"], "erro", f"teste: {exc}")
            return
        log.info("Teste do lembrete #%s enviado para %s", reminder["id"], user)
        db.log_send(reminder["id"], "teste", f"enviado só para {user.display_name}")

    def resolve_recipients(self, recipients: List[Dict[str, Any]]) -> Dict[int, str]:
        """Expande cargos nos seus membros e remove duplicados. Retorna {user_id: nome}."""
        resolved: Dict[int, str] = {}
        for r in recipients:
            if r["type"] == "user":
                member = next((g.get_member(int(r["id"])) for g in self.guilds
                               if g.get_member(int(r["id"]))), None)
                resolved[int(r["id"])] = member.display_name if member else r.get("name", r["id"])
            elif r["type"] == "role":
                for guild in self.guilds:
                    role = guild.get_role(int(r["id"]))
                    if role:
                        for member in role.members:
                            if not member.bot:
                                resolved[member.id] = member.display_name
        return resolved

    @staticmethod
    def build_message(reminder: Dict[str, Any], name: str = "pessoal",
                      mentions: bool = True) -> Dict[str, Any]:
        now = datetime.now(TZ)
        text = apply_variables(reminder["content"], now, name)
        mention_str = mention_tokens(reminder["mentions"]) if mentions else ""
        kwargs: Dict[str, Any] = {
            "allowed_mentions": discord.AllowedMentions(everyone=True, roles=True, users=True),
        }
        if reminder.get("title"):
            kwargs["embed"] = discord.Embed(
                title=apply_variables(reminder["title"], now, name),
                description=text,
                color=discord.Color.blurple(),
            )
            if mention_str:
                kwargs["content"] = mention_str
        else:
            kwargs["content"] = f"{mention_str}\n{text}".strip()
        if reminder["links"]:
            kwargs["view"] = LinksView(reminder["links"])
        return kwargs

    @dispatch_loop.before_loop
    async def _before_dispatch(self) -> None:
        await self.wait_until_ready()

    # --- Cache para a CLI --------------------------------------------------

    @tasks.loop(minutes=10)
    async def sync_cache_loop(self) -> None:
        """Salva canais, cargos e membros no banco para a CLI oferecer como opções."""
        channels, roles, members = [], [], []
        for guild in self.guilds:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    channels.append({"id": str(ch.id), "name": ch.name, "guild": guild.name})
            for role in guild.roles:
                if not role.is_default() and not role.managed:
                    roles.append({"id": str(role.id), "name": role.name, "guild": guild.name})
            if MEMBERS_INTENT:
                for member in guild.members:
                    if not member.bot:
                        members.append({"id": str(member.id), "name": member.display_name,
                                        "guild": guild.name})
        db.replace_cache("channel", channels)
        db.replace_cache("role", roles)
        if MEMBERS_INTENT:
            db.replace_cache("member", members)
        log.info("Cache atualizado: %d canais, %d cargos, %d membros",
                 len(channels), len(roles), len(members))

    @sync_cache_loop.before_loop
    async def _before_sync(self) -> None:
        await self.wait_until_ready()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if not TOKEN:
        sys.exit("DISCORD_TOKEN não configurado. Copie .env.example para .env e preencha o token.")
    try:
        ReminderBot().run(TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        sys.exit("O Discord recusou o 'Server Members Intent'. Ligue-o no Developer Portal "
                 "(aba Bot > Privileged Gateway Intents) ou use MEMBERS_INTENT=false no .env.")
