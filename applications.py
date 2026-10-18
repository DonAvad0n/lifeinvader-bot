from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import config
import notices
from auditlog import AuditLog
from store import Application, Store

log = logging.getLogger("lifeinvader.applications")

PANEL_TEXT = (
    "Чтобы подать заявку на получение начальных ролей Lifeinvader, нажмите кнопку ниже"
    " и заполните форму.\n\n"
    "Заявки, составленные не по форме, могут быть отклонены.\n\n"
    "Если вам нужны дополнительные должностные или служебные роли, обратитесь"
    " к руководству Lifeinvader.\n"
    "Если вы новый куратор фракции, пинганите Главного Куратора государственных"
    " фракций или его заместителя в любом другом канале."
)

NAME_RE = re.compile(
    r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-]*\s+[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-]*$"
)
STATIC_RE = re.compile(r"^\d{1,10}$")

STATUS_TITLES = {
    "pending": ("НА РАССМОТРЕНИИ", 0xF1C40F),
    "approved": ("ОДОБРЕНА", 0x2ECC71),
    "declined": ("ОТКЛОНЕНА", 0xE74C3C),
}


def validate_form(name: str, static: str) -> str | None:
    if not name:
        return "Укажите имя и фамилию."
    if not NAME_RE.match(name):
        return "Имя и фамилия — два слова, только буквы и дефис, без цифр и лишних знаков."
    if not static:
        return "Укажите статик персонажа."
    if not STATIC_RE.match(static):
        return "Статик — это только цифры, до 10 знаков."
    if len(f"{name} | {static}") > 32:
        return "Слишком длинные данные для ника: имя, фамилия и статик вместе не длиннее 32 символов."
    return None


class ApplicationModal(discord.ui.Modal, title="Заявка на получение ролей"):
    applicant_name = discord.ui.TextInput(
        label="Имя и фамилия",
        placeholder="Например: Tony Jonse",
        max_length=40,
        required=True,
    )
    applicant_static = discord.ui.TextInput(
        label="Статик",
        placeholder="Например: 154980",
        max_length=10,
        required=True,
    )

    def __init__(self, service: "ApplicationsService") -> None:
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.submit(
            interaction, str(self.applicant_name.value), str(self.applicant_static.value)
        )


class ApplyPanelView(discord.ui.View):
    def __init__(self, service: "ApplicationsService") -> None:
        super().__init__(timeout=None)
        self.service = service
        button = discord.ui.Button(
            label="Подать заявку на роли",
            style=discord.ButtonStyle.danger,
            custom_id="apply:open",
        )
        button.callback = self._on_apply
        self.add_item(button)

    async def _on_apply(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ApplicationModal(self.service))


class ReviewView(discord.ui.View):
    def __init__(self, service: "ApplicationsService") -> None:
        super().__init__(timeout=None)
        self.service = service

        approve = discord.ui.Button(
            label="Одобрить",
            style=discord.ButtonStyle.success,
            custom_id="apply:approve",
        )
        approve.callback = self._on_approve
        self.add_item(approve)

        decline = discord.ui.Button(
            label="Отклонить",
            style=discord.ButtonStyle.danger,
            custom_id="apply:decline",
        )
        decline.callback = self._on_decline
        self.add_item(decline)

    async def _on_approve(self, interaction: discord.Interaction) -> None:
        await self.service.approve(interaction)

    async def _on_decline(self, interaction: discord.Interaction) -> None:
        await self.service.decline(interaction)


class ApplicationsService:
    def __init__(self, store: Store, tz: ZoneInfo, audit_log: AuditLog) -> None:
        self.store = store
        self.tz = tz
        self.audit_log = audit_log

    @staticmethod
    def _display(user: discord.abc.User) -> str:
        return (
            getattr(user, "display_name", None)
            or getattr(user, "name", None)
            or str(user.id)
        )

    def review_embed(
        self,
        app: Application,
        status: str | None = None,
        reviewer: discord.abc.User | None = None,
    ) -> discord.Embed:
        status = status or app.status
        label, color = STATUS_TITLES.get(status, STATUS_TITLES["pending"])

        lines = [
            f"Заявка от <@{app.applicant_id}>.",
            f"Discord ID: `{app.applicant_id}`",
            "",
            "Уважаемый сотрудник Lifeinvader!",
            "",
            "Обратите внимание на то, как записаны Имя Фамилия и статик персонажа!",
            "Проверьте данные дважды перед тем, как одобрить заявку!",
            "",
            "Пользователь оставил следующие данные:",
            f"**Имя Фамилия:** {app.name}",
            f"**Статик:** {app.static}",
        ]
        if reviewer is not None and status == "approved":
            lines += ["", f"**Одобрил(-а):** {reviewer.mention}"]
        elif reviewer is not None and status == "declined":
            lines += ["", f"**Отклонил(-а):** {reviewer.mention}"]

        embed = discord.Embed(
            title=f"Заявка на выдачу ролей — {label}",
            color=color,
            description="\n".join(lines),
        )
        embed.set_footer(
            text=f"Заявка №{app.id} • Lifeinvader Helper • {app.created_at}"
        )
        return embed

    def audit_embed(self, app: Application, reviewer: discord.abc.User) -> discord.Embed:
        embed = discord.Embed(title="Кадровый аудит • Принятие", color=0x2ECC71)
        reviewer_name = (
            getattr(reviewer, "display_name", None)
            or getattr(reviewer, "name", None)
            or str(reviewer.id)
        )
        embed.add_field(
            name="Принял(-а):",
            value=f"{reviewer.mention}\n`{reviewer_name}`",
            inline=False,
        )
        embed.add_field(
            name="Принят(-а):",
            value=(
                f"<@{app.applicant_id}>\n`{app.name}`\n`{app.static}`"
            ),
            inline=False,
        )
        embed.add_field(name="Номер ID карты:", value=f"`{app.static}`", inline=True)
        embed.add_field(name="Действие:", value=config.APPLY_ACTION_TEXT, inline=True)
        embed.add_field(name="Причина:", value=config.APPLY_REASON_TEXT, inline=True)
        embed.set_footer(
            text=f"Lifeinvader Helper • {datetime.now(self.tz):%d.%m.%Y %H:%M}"
        )
        return embed

    async def ensure_panel(self, bot: discord.Client) -> None:
        channel = bot.get_channel(config.APPLY_PANEL_CHANNEL_ID)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("Канал выдачи ролей %s недоступен", config.APPLY_PANEL_CHANNEL_ID)
            return
        stored = self.store.get_setting("apply_panel_message")
        if stored and stored.isdigit():
            try:
                message = await channel.fetch_message(int(stored))
                await message.edit(content=PANEL_TEXT, view=ApplyPanelView(self))
                return
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                log.warning("Не удалось обновить панель заявок: %s", exc)
        message = await channel.send(PANEL_TEXT, view=ApplyPanelView(self))
        self.store.set_setting("apply_panel_message", str(message.id))
        log.info("Панель заявок опубликована в канале %s", channel.id)

    async def submit(
        self, interaction: discord.Interaction, raw_name: str, raw_static: str
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await notices.reply(interaction, 
                "Заявку можно подать только на сервере.", ephemeral=True
            )
            return

        name = " ".join((raw_name or "").split())
        static = (raw_static or "").strip()
        error = validate_form(name, static)
        if error is not None:
            await notices.reply(interaction, f"⚠️ {error}", ephemeral=True)
            return

        if self.store.pending_application(guild.id, interaction.user.id) is not None:
            await notices.reply(interaction, 
                "У вас уже есть заявка на рассмотрении — дождитесь ответа руководства.",
                ephemeral=True,
            )
            return

        channel = guild.get_channel(config.APPLY_REVIEW_CHANNEL_ID)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("Канал заявок %s недоступен", config.APPLY_REVIEW_CHANNEL_ID)
            await notices.reply(interaction, 
                "Канал для заявок не найден — сообщите руководству.", ephemeral=True
            )
            return

        draft = Application(
            id=0,
            guild_id=guild.id,
            applicant_id=interaction.user.id,
            name=name,
            static=static,
            status="pending",
            channel_id=channel.id,
            message_id=0,
            reviewer_id=None,
            created_at=datetime.now(self.tz).strftime("%d.%m.%Y %H:%M"),
            decided_at=None,
        )
        message = await channel.send(embed=self.review_embed(draft), view=ReviewView(self))
        application_id = self.store.create_application(
            guild.id, interaction.user.id, name, static, channel.id, message.id
        )
        await notices.reply(interaction, 
            f"Заявка №{application_id} отправлена на рассмотрение. "
            f"Имя в заявке: **{name}**, статик: **{static}**.",
            ephemeral=True,
        )

    def _resolve(self, interaction: discord.Interaction) -> Application | None:
        if interaction.message is None:
            return None
        return self.store.application_by_message(interaction.message.id)

    @staticmethod
    def _can_review(interaction: discord.Interaction) -> bool:
        permissions = interaction.permissions
        return bool(permissions.administrator or permissions.manage_roles)

    async def approve(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        app = self._resolve(interaction)
        if app is None:
            await notices.reply(interaction, "Заявка не найдена в базе.", ephemeral=True)
            return
        if app.status != "pending":
            await notices.reply(interaction, 
                "Эта заявка уже обработана.", ephemeral=True
            )
            return
        if not self._can_review(interaction):
            await notices.reply(interaction, 
                "Одобрять заявки могут только участники с правом «Управление ролями».",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await notices.reply(interaction, "Только на сервере.", ephemeral=True)
            return

        try:
            member = guild.get_member(app.applicant_id) or await guild.fetch_member(
                app.applicant_id
            )
        except discord.HTTPException:
            await notices.reply(interaction, 
                "Не удалось найти участника на сервере — возможно, он вышел.",
                ephemeral=True,
            )
            return

        nickname = f"{app.name} | {app.static}"
        reason = f"Заявка №{app.id} одобрена {interaction.user}"
        problems: list[str] = []

        try:
            await member.edit(nick=nickname, reason=reason)
        except discord.Forbidden:
            problems.append(
                "не удалось сменить ник (нужно право «Управление никнеймами» "
                "и роль бота выше роли участника)"
            )
        except discord.HTTPException as exc:
            problems.append(f"Discord отклонил смену ника (HTTP {exc.status})")

        role = guild.get_role(config.TRAINEE_ROLE_ID)
        if role is None:
            problems.append("роль «Стажёр» не найдена на сервере")
        else:
            try:
                await member.add_roles(role, reason=reason)
            except discord.Forbidden:
                problems.append(
                    "не удалось выдать роль (нужно право «Управление ролями» "
                    "и роль бота выше роли «Стажёр»)"
                )
            except discord.HTTPException as exc:
                problems.append(f"Discord отклонил выдачу роли (HTTP {exc.status})")

        if problems:
            await notices.reply(interaction, 
                "⚠️ Заявка осталась на рассмотрении: " + "; ".join(problems) + ".",
                ephemeral=True,
            )
            return

        self.store.set_application_status(app.id, "approved", interaction.user.id)
        self.audit_log.append(
            application_id=app.id,
            decision="Одобрено",
            name=app.name,
            static=app.static,
            nickname=nickname,
            applicant=getattr(member, "display_name", None) or str(app.applicant_id),
            applicant_id=app.applicant_id,
            reviewer=self._display(interaction.user),
            reviewer_id=interaction.user.id,
        )
        approved = replace(app, status="approved")
        await interaction.edit_original_response(
            embed=self.review_embed(approved, reviewer=interaction.user), view=None
        )

        audit_channel = guild.get_channel(config.APPLY_AUDIT_CHANNEL_ID)
        if isinstance(audit_channel, discord.abc.Messageable):
            try:
                await audit_channel.send(embed=self.audit_embed(app, interaction.user))
            except discord.HTTPException as exc:
                log.warning("Не удалось записать кадровый аудит: %s", exc)
                await notices.reply(interaction, 
                    "Заявка одобрена, но запись в кадровый аудит не отправилась — "
                    "проверьте права бота в том канале.",
                    ephemeral=True,
                )
                return
        else:
            log.warning("Канал аудита %s недоступен", config.APPLY_AUDIT_CHANNEL_ID)

        await notices.reply(interaction, 
            f"Одобрено: **{nickname}**, роль «Стажёр» выдана.", ephemeral=True
        )

    async def decline(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        app = self._resolve(interaction)
        if app is None:
            await notices.reply(interaction, "Заявка не найдена в базе.", ephemeral=True)
            return
        if app.status != "pending":
            await notices.reply(interaction, 
                "Эта заявка уже обработана.", ephemeral=True
            )
            return
        if not self._can_review(interaction):
            await notices.reply(interaction, 
                "Отклонять заявки могут только участники с правом «Управление ролями».",
                ephemeral=True,
            )
            return

        self.store.set_application_status(app.id, "declined", interaction.user.id)
        member = interaction.guild.get_member(app.applicant_id) if interaction.guild else None
        if member is None and interaction.guild is not None:
            fetcher = getattr(interaction.guild, "fetch_member", None)
            if fetcher is not None:
                try:
                    member = await fetcher(app.applicant_id)
                except discord.HTTPException:
                    member = None
        self.audit_log.append(
            application_id=app.id,
            decision="Отклонено",
            name=app.name,
            static=app.static,
            nickname="",
            applicant=(
                getattr(member, "display_name", None) if member is not None else None
            )
            or str(app.applicant_id),
            applicant_id=app.applicant_id,
            reviewer=self._display(interaction.user),
            reviewer_id=interaction.user.id,
        )
        declined = replace(app, status="declined")
        await interaction.edit_original_response(
            embed=self.review_embed(declined, reviewer=interaction.user), view=None
        )
        await notices.reply(interaction, "Заявка отклонена.", ephemeral=True)
