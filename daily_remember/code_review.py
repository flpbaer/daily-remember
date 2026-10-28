"""Code reviews registrados pelo time com /code-review.

Fluxo: alguém usa /code-review no canal e preenche o formulário; o bot publica um
card no canal e manda o CR no privado do revisor (o usuário de ./dr me) com botões
de status. Cada mudança de status atualiza o card e avisa o autor no canal. Quando
o revisor pede ajustes, o card ganha um botão para o autor devolver o CR, o que
abre uma nova rodada e manda o CR de novo para o revisor.

Os botões usam custom_id fixo (DynamicItem), então continuam funcionando depois
que o bot reinicia.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List
from urllib.parse import urlparse

import discord
from discord import app_commands

from . import db
from .config import TZ

log = logging.getLogger("daily_remember")

KINDS = {
    "new": "🆕 Implementação nova",
    "adjustment": "🔧 Ajuste",
    "bugfix": "🐛 Correção de bug",
    "after_review": "🔁 Voltou de ajuste do code review",
    "reopened": "↩️ Voltou (reaberta)",
    "other": "📝 Outro",
}

STATUSES = {
    "pending": ("Pendente", "⏳", discord.Color.orange()),
    "in_review": ("Em revisão", "🔍", discord.Color.blue()),
    "changes_requested": ("Ajustes solicitados", "✏️", discord.Color.gold()),
    "approved": ("Aprovado", "✅", discord.Color.green()),
    "reviewed": ("Revisado", "💬", discord.Color.blurple()),
    "canceled": ("Cancelado", "🚫", discord.Color.dark_grey()),
}

# Botões que o revisor vê no privado
OWNER_ACTIONS = ("in_review", "approved", "changes_requested", "reviewed", "canceled")

Review = Dict[str, Any]


def status_text(status: str) -> str:
    label, emoji, _ = STATUSES[status]
    return f"{emoji} {label}"


def is_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_reviewer(user_id: int) -> bool:
    return db.get_setting("my_user_id") == str(user_id)


def jump_url(review: Review) -> str:
    return (f"https://discord.com/channels/{review['guild_id']}/{review['channel_id']}/"
            f"{review['channel_message_id']}")


# --- Card e botões ---------------------------------------------------------

def review_embed(review: Review) -> discord.Embed:
    _, _, color = STATUSES[review["status"]]
    embed = discord.Embed(
        title=f"Code review #{review['id']} — {KINDS.get(review['kind'], review['kind'])}",
        description=review["description"],
        color=color,
    )
    embed.add_field(name="Status", value=status_text(review["status"]))
    embed.add_field(name="Autor", value=f"<@{review['author_id']}>")
    if review["round"] > 1:
        embed.add_field(name="Rodada", value=str(review["round"]))
    embed.add_field(name="Tarefa", value=review["task_url"], inline=False)
    embed.add_field(name="PR", value=review["pr_url"], inline=False)
    history = []
    for event in review["events"][-6:]:
        when = db.from_iso(event["at"]).astimezone(TZ).strftime("%d/%m %H:%M")
        line = f"`{when}` {status_text(event['status'])} — {event['by_name']}"
        if event["note"]:
            line += f": {event['note']}"
        history.append(line)
    embed.add_field(name="Histórico", value="\n".join(history)[-1024:], inline=False)
    return embed


def _link_buttons(review: Review) -> List[discord.ui.Button]:
    buttons = []
    for label, url in (("Tarefa", review["task_url"]), ("PR", review["pr_url"])):
        if is_url(url):
            buttons.append(discord.ui.Button(label=label, url=url.strip()))
    return buttons


def reviewer_view(review: Review) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for status in OWNER_ACTIONS:
        view.add_item(StatusButton(review["id"], status, current=review["status"] == status))
    for button in _link_buttons(review):
        view.add_item(button)
    return view


def channel_view(review: Review) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for button in _link_buttons(review):
        view.add_item(button)
    if review["status"] == "changes_requested":
        view.add_item(ResubmitButton(review["id"]))
    return view


class StatusButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"cr:(?P<id>\d+):(?P<status>[a-z_]+)"):
    def __init__(self, review_id: int, status: str, current: bool = False) -> None:
        label, emoji, _ = STATUSES[status]
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, custom_id=f"cr:{review_id}:{status}", disabled=current,
            style=discord.ButtonStyle.success if current else discord.ButtonStyle.secondary,
        ))
        self.review_id, self.status = review_id, status

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["id"]), match["status"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await change_status(interaction, self.review_id, self.status)


class ResubmitButton(discord.ui.DynamicItem[discord.ui.Button], template=r"cr-back:(?P<id>\d+)"):
    def __init__(self, review_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Ajustes feitos", emoji="🔁", style=discord.ButtonStyle.primary,
            custom_id=f"cr-back:{review_id}",
        ))
        self.review_id = review_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        review = db.get_review(self.review_id)
        if not review:
            await interaction.response.send_message("Esse code review não existe mais.", ephemeral=True)
            return
        if str(interaction.user.id) != review["author_id"] and not is_reviewer(interaction.user.id):
            await interaction.response.send_message("Só o autor do code review pode devolvê-lo.",
                                                    ephemeral=True)
            return
        if review["status"] != "changes_requested":
            await interaction.response.send_message("Esse code review não está aguardando ajustes.",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(ResubmitModal(self.review_id))


# --- Ações -----------------------------------------------------------------

async def change_status(interaction: discord.Interaction, review_id: int, status: str) -> None:
    """Clique do revisor num botão de status no privado."""
    if not is_reviewer(interaction.user.id):
        await interaction.response.send_message("Só o revisor pode mudar o status.", ephemeral=True)
        return
    review = db.get_review(review_id)
    if not review:
        await interaction.response.send_message("Esse code review não existe mais.", ephemeral=True)
        return
    if review["status"] == status:
        await interaction.response.send_message(f"Já está como {status_text(status)}.", ephemeral=True)
        return
    db.add_review_event(review_id, status, interaction.user.display_name)
    review = db.get_review(review_id)
    await interaction.response.edit_message(embed=review_embed(review), view=reviewer_view(review))
    await update_channel_card(interaction.client, review)
    await notify_author(interaction.client, review)


async def update_channel_card(client: discord.Client, review: Review) -> None:
    if not review["channel_message_id"]:
        return
    try:
        message = client.get_partial_messageable(int(review["channel_id"])).get_partial_message(
            int(review["channel_message_id"]))
        await message.edit(embed=review_embed(review), view=channel_view(review))
    except Exception as exc:
        log.error("Não consegui atualizar o card do code review #%s: %s", review["id"], exc)


async def notify_author(client: discord.Client, review: Review) -> None:
    text = f"<@{review['author_id']}> seu code review #{review['id']} agora está **{status_text(review['status'])}**."
    if review["status"] == "changes_requested":
        text += " Quando terminar, clique em **🔁 Ajustes feitos** no card."
    try:
        channel = client.get_partial_messageable(int(review["channel_id"]))
        await channel.send(f"{text}\n{jump_url(review)}",
                           allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    except Exception as exc:
        log.error("Não consegui avisar o autor do code review #%s: %s", review["id"], exc)


async def send_to_reviewer(client: discord.Client, review: Review, intro: str) -> None:
    """Manda o CR no privado do revisor. Uma mensagem anterior do mesmo CR perde os botões."""
    owner_id = db.get_setting("my_user_id")
    if not owner_id:
        log.warning("Code review #%s registrado, mas nenhum revisor definido (use ./dr me)", review["id"])
        return
    try:
        user = client.get_user(int(owner_id)) or await client.fetch_user(int(owner_id))
        message = await user.send(content=intro, embed=review_embed(review), view=reviewer_view(review))
    except Exception as exc:
        log.error("Não consegui mandar o code review #%s para o revisor: %s", review["id"], exc)
        return
    if review["owner_message_id"]:
        try:
            old = client.get_partial_messageable(int(review["owner_channel_id"])).get_partial_message(
                int(review["owner_message_id"]))
            await old.edit(content=f"↪️ Substituído pela rodada {review['round']} (mensagem abaixo).",
                           view=None)
        except Exception as exc:
            log.warning("Não consegui limpar a mensagem antiga do code review #%s: %s", review["id"], exc)
    db.set_review_owner_message(review["id"], message.channel.id, message.id)


# --- Formulários -----------------------------------------------------------

class ReviewModal(discord.ui.Modal):
    task_url = discord.ui.TextInput(label="Link da tarefa", placeholder="https://...", max_length=300)
    pr_url = discord.ui.TextInput(label="Link do PR", placeholder="https://github.com/.../pull/123",
                                  max_length=300)
    description = discord.ui.TextInput(
        label="Descrição", style=discord.TextStyle.paragraph, max_length=1000,
        placeholder="O que mudou e onde focar a revisão",
    )

    def __init__(self, kind: str) -> None:
        super().__init__(title=f"Code review: {KINDS[kind]}"[:45])
        self.kind = kind

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_url(self.pr_url.value):
            await interaction.response.send_message(
                "O link do PR precisa começar com http:// ou https://. Seus dados, para copiar:\n"
                f"```\nTarefa: {self.task_url.value}\nPR: {self.pr_url.value}\n\n{self.description.value}\n```",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        author = interaction.user
        review_id = db.create_review(
            guild_id=interaction.guild_id, channel_id=interaction.channel_id, author_id=author.id,
            author_name=author.display_name, kind=self.kind, task_url=self.task_url.value.strip(),
            pr_url=self.pr_url.value.strip(), description=self.description.value.strip(),
        )
        review = db.get_review(review_id)
        card = await interaction.channel.send(embed=review_embed(review), view=channel_view(review))
        db.set_review_channel_message(review_id, card.id)
        await interaction.followup.send(f"✅ Code review #{review_id} registrado! Você será avisado aqui "
                                        "quando o status mudar.", ephemeral=True)
        await send_to_reviewer(interaction.client, db.get_review(review_id),
                               f"📥 Novo code review de **{author.display_name}**")


class ResubmitModal(discord.ui.Modal, title="Devolver para revisão"):
    note = discord.ui.TextInput(label="O que foi ajustado? (opcional)", style=discord.TextStyle.paragraph,
                                required=False, max_length=500)

    def __init__(self, review_id: int) -> None:
        super().__init__()
        self.review_id = review_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        review = db.get_review(self.review_id)
        if not review or review["status"] != "changes_requested":
            await interaction.response.send_message("Esse code review não está aguardando ajustes.",
                                                    ephemeral=True)
            return
        db.add_review_event(self.review_id, "pending", interaction.user.display_name,
                            note=self.note.value.strip(), new_round=True)
        review = db.get_review(self.review_id)
        await interaction.response.edit_message(embed=review_embed(review), view=channel_view(review))
        await send_to_reviewer(
            interaction.client, review,
            f"🔁 **{interaction.user.display_name}** devolveu o code review #{review['id']} "
            f"após ajustes (rodada {review['round']})",
        )


# --- Slash command ---------------------------------------------------------

@app_commands.command(name="code-review", description="Registrar um code review pendente para o revisor")
@app_commands.describe(tipo="Que tipo de mudança vai para revisão?")
@app_commands.choices(tipo=[app_commands.Choice(name=label, value=key) for key, label in KINDS.items()])
@app_commands.guild_only()
async def code_review_command(interaction: discord.Interaction, tipo: app_commands.Choice[str]) -> None:
    allowed_channel = db.get_setting("review_channel_id")
    if allowed_channel and str(interaction.channel_id) != allowed_channel:
        await interaction.response.send_message(f"Use este comando no canal <#{allowed_channel}>.",
                                                ephemeral=True)
        return
    await interaction.response.send_modal(ReviewModal(tipo.value))
