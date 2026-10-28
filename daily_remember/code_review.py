"""Code reviews registrados pelo time, no estilo "ticket".

Fluxo: no canal de code review fica um painel com o botão "Registrar code review"
(ou a pessoa usa /code-review). O formulário pede tipo, tarefa, PR e descrição. O bot
abre uma thread privada só com o autor e o revisor (o usuário de ./dr me), publica o
card nela e manda o CR no privado do revisor com botões de status. Cada mudança de
status atualiza o card e avisa o autor na thread, sem poluir o canal. Quando o
revisor pede ajustes, o card ganha um botão para o autor devolver o CR, o que abre
uma nova rodada. Ao aprovar ou cancelar, a thread é arquivada.

Os botões usam custom_id fixo (DynamicItem), então continuam funcionando depois
que o bot reinicia.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
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
# Status que encerram o ticket (a thread é arquivada; volta a abrir se o status mudar)
CLOSED_STATUSES = ("approved", "canceled")

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
    await sync_ticket(interaction.client, review)


async def get_ticket(client: discord.Client, review: Review) -> Optional[discord.Thread]:
    """A thread do CR, ou None se o card está num canal comum (ou a thread sumiu)."""
    channel_id = int(review["channel_id"])
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.Thread) else None


async def sync_ticket(client: discord.Client, review: Review) -> None:
    """Depois de mudar o status: reabre a thread se preciso, atualiza o card, avisa o autor
    e arquiva a thread quando o CR termina."""
    thread = await get_ticket(client, review)
    if thread and (thread.archived or thread.locked):
        try:
            await thread.edit(archived=False, locked=False)
        except discord.HTTPException as exc:
            log.warning("Não consegui reabrir a thread do code review #%s: %s", review["id"], exc)
    await update_channel_card(client, review)
    await notify_author(client, review)
    if thread and review["status"] in CLOSED_STATUSES:
        await close_ticket(thread, review)


async def close_ticket(thread: discord.Thread, review: Review) -> None:
    try:
        await thread.send(f"🔒 Code review finalizado como **{status_text(review['status'])}**. "
                          "Este chat foi arquivado; ele volta a abrir se o status mudar.")
        await thread.edit(archived=True, locked=True)
    except discord.Forbidden:
        # Trancar exige "Gerenciar threads"; sem ela, só arquiva
        try:
            await thread.edit(archived=True)
        except discord.HTTPException as exc:
            log.warning("Não consegui arquivar a thread do code review #%s: %s", review["id"], exc)
    except discord.HTTPException as exc:
        log.warning("Não consegui arquivar a thread do code review #%s: %s", review["id"], exc)


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
    if review["status"] == "in_review":
        text += " 👀"
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

class ReviewModal(discord.ui.Modal, title="Registrar code review"):
    kind = discord.ui.Label(text="Tipo", component=discord.ui.Select(
        placeholder="Que tipo de mudança vai para revisão?",
        options=[discord.SelectOption(label=label, value=key) for key, label in KINDS.items()],
    ))
    task_url = discord.ui.Label(text="Link da tarefa", component=discord.ui.TextInput(
        placeholder="https://...", max_length=300))
    pr_url = discord.ui.Label(text="Link do PR", component=discord.ui.TextInput(
        placeholder="https://github.com/.../pull/123", max_length=300))
    description = discord.ui.Label(text="Descrição", component=discord.ui.TextInput(
        style=discord.TextStyle.paragraph, max_length=1000,
        placeholder="O que mudou e onde focar a revisão",
    ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        kind = self.kind.component.values[0]
        task_url = self.task_url.component.value.strip()
        pr_url = self.pr_url.component.value.strip()
        description = self.description.component.value.strip()
        if not is_url(pr_url):
            await interaction.response.send_message(
                "O link do PR precisa começar com http:// ou https://. Seus dados, para copiar:\n"
                f"```\nTarefa: {task_url}\nPR: {pr_url}\n\n{description}\n```",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        author = interaction.user
        review_id = db.create_review(
            guild_id=interaction.guild_id, channel_id=interaction.channel_id, author_id=author.id,
            author_name=author.display_name, kind=kind, task_url=task_url, pr_url=pr_url,
            description=description,
        )
        review = db.get_review(review_id)
        target = await open_ticket(interaction, review)
        card = await target.send(
            content=f"<@{author.id}> seu code review foi registrado. O andamento aparece aqui.",
            embed=review_embed(review), view=channel_view(review),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        db.set_review_card(review_id, target.id, card.id)
        await interaction.followup.send(f"✅ Code review #{review_id} registrado! Acompanhe em {target.mention}.",
                                        ephemeral=True)
        await send_to_reviewer(interaction.client, db.get_review(review_id),
                               f"📥 Novo code review de **{author.display_name}**")


async def open_ticket(interaction: discord.Interaction, review: Review) -> discord.abc.Messageable:
    """Cria a thread privada do CR com o autor e o revisor. Se não der, usa o próprio canal."""
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        channel = channel.parent
    try:
        thread = await channel.create_thread(
            name=f"CR #{review['id']} · {review['author_name']} · {KINDS[review['kind']]}"[:100],
            type=discord.ChannelType.private_thread, invitable=False, auto_archive_duration=10080,
        )
    except (discord.HTTPException, AttributeError) as exc:
        log.warning("Não consegui criar a thread do code review #%s (%s); o card vai no canal",
                    review["id"], exc)
        return interaction.channel
    members = {interaction.user.id}
    owner_id = db.get_setting("my_user_id")
    if owner_id:
        members.add(int(owner_id))
    for user_id in members:
        try:
            await thread.add_user(discord.Object(user_id))
        except discord.HTTPException as exc:
            log.warning("Não consegui adicionar %s à thread do code review #%s: %s", user_id, review["id"], exc)
    return thread


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


# --- Painel do canal -------------------------------------------------------

class NewReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"cr-new"):
    def __init__(self) -> None:
        super().__init__(discord.ui.Button(
            label="Registrar code review", emoji="📝", style=discord.ButtonStyle.primary, custom_id="cr-new",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ReviewModal())


def panel_message() -> Dict[str, Any]:
    embed = discord.Embed(
        title="📋 Code review",
        description=(
            "Precisa de revisão? Clique em **Registrar code review** e preencha o tipo, a tarefa, "
            "o PR e uma descrição.\n\n"
            "O bot abre um chat privado só entre você e o revisor. O andamento (em revisão, "
            "ajustes, aprovado) aparece lá, e o chat é arquivado quando o code review termina."
        ),
        color=discord.Color.blurple(),
    )
    view = discord.ui.View(timeout=None)
    view.add_item(NewReviewButton())
    return {"embed": embed, "view": view}


async def ensure_panel(client: discord.Client) -> None:
    """Mantém o painel publicado no canal de code review (setting review_channel_id).

    O painel publicado fica salvo em review_panel ("canal:mensagem"). A CLI pede uma nova
    publicação marcando review_panel_refresh; o painel antigo é apagado.
    """
    channel_id = db.get_setting("review_channel_id") or ""
    panel = db.get_setting("review_panel") or ""
    refresh = db.get_setting("review_panel_refresh") == "1"
    if panel.split(":")[0] == channel_id and not refresh:
        return
    db.set_setting("review_panel_refresh", "")
    old_channel, _, old_message = panel.partition(":")
    if old_message:
        try:
            await client.get_partial_messageable(int(old_channel)).get_partial_message(int(old_message)).delete()
        except discord.HTTPException:
            pass
    if not channel_id:
        db.set_setting("review_panel", "")
        return
    try:
        channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
        message = await channel.send(**panel_message())
    except Exception as exc:
        # Salva sem mensagem para não tentar de novo a cada ciclo; ./dr review-channel tenta outra vez
        log.error("Não consegui publicar o painel de code review em %s: %s", channel_id, exc)
        db.set_setting("review_panel", f"{channel_id}:")
        return
    db.set_setting("review_panel", f"{channel_id}:{message.id}")
    log.info("Painel de code review publicado em #%s", getattr(channel, "name", channel_id))


# --- Slash command ---------------------------------------------------------

@app_commands.command(name="code-review", description="Registrar um code review pendente para o revisor")
@app_commands.guild_only()
async def code_review_command(interaction: discord.Interaction) -> None:
    allowed_channel = db.get_setting("review_channel_id")
    if allowed_channel and str(interaction.channel_id) != allowed_channel:
        await interaction.response.send_message(f"Use o painel do canal <#{allowed_channel}>.",
                                                ephemeral=True)
        return
    await interaction.response.send_modal(ReviewModal())
