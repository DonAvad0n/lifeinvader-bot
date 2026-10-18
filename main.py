from __future__ import annotations

import asyncio
import functools
import logging
import math
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from applications import ApplicationsService, ApplyPanelView, ReviewView
from auditlog import AuditLog
from sheet import WEEKDAYS, DayGrid, SheetCache, SheetError, Slot
from store import Claim, Store

SLOTS_PER_PAGE = 20
SLOT_ROWS = 4
NAV_ROW = SLOT_ROWS
PANEL_COLOR = discord.Color.from_rgb(0x2E, 0xC4, 0xB6)
TITLE_LIMIT = 64
DESCRIPTION_LIMIT = 3900
WAVE_FORMS = ("волна", "волны", "волн")

log = logging.getLogger("efiry")


def plural(count: int, forms: tuple[str, str, str]) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return forms[0]
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return forms[1]
    return forms[2]


def pages_count(total: int) -> int:
    return max(1, math.ceil(total / SLOTS_PER_PAGE))


def clamp_page(page: int, total: int) -> int:
    return max(0, min(page, pages_count(total) - 1))


def align_page(total: int, claims: dict[int, Claim]) -> int:
    if not claims:
        return 0
    return clamp_page((max(claims) + 1) // SLOTS_PER_PAGE, total)


def shorten(text: str, limit: int = TITLE_LIMIT) -> str:
    text = (text or "").strip()
    if not text:
        return "—"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def user_label(user: discord.abc.User) -> str:
    name = getattr(user, "display_name", None) or user.name or str(user.id)
    name = name.replace("@", "@\u200b")
    return discord.utils.escape_markdown(name)[:48]


def slot_start(day: date, time_text: str, tz: ZoneInfo) -> datetime | None:
    parts = time_text.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def next_free_index(
    grid: DayGrid, claims: dict[int, Claim], now: datetime, tz: ZoneInfo
) -> int | None:
    fallback: int | None = None
    for index, slot in enumerate(grid.slots):
        if index in claims:
            continue
        if fallback is None:
            fallback = index
        start = slot_start(grid.day, slot.time, tz)
        if start is not None and start >= now:
            return index
    return fallback


def describe(
    grid: DayGrid, claims: dict[int, Claim], page: int, now: datetime, tz: ZoneInfo
) -> discord.Embed:
    total = len(grid.slots)
    pages = pages_count(total)
    next_free = next_free_index(grid, claims, now, tz)

    lines: list[str] = []
    if next_free is None:
        lines.append("⏭ Свободных волн на сегодня не осталось")
    else:
        slot = grid.slots[next_free]
        lines.append(
            f"⏭ Ближайшая свободная: `#{next_free + 1}` **{slot.time}** · {shorten(slot.title)}"
        )
    lines.append("")

    for index, slot in enumerate(grid.slots):
        claim = claims.get(index)
        status = f"🔴 **занято** · {claim.user_name}" if claim else "🟢 свободно"
        lines.append(f"`#{index + 1}` **{slot.time}** · {shorten(slot.title)} — {status}")

    description = "\n".join(lines)
    if len(description) > DESCRIPTION_LIMIT:
        description = description[: DESCRIPTION_LIMIT - 1] + "…"

    free = total - len(claims)
    embed = discord.Embed(
        title=f"Эфиры · {grid.day:%d.%m.%Y}, {grid.weekday}",
        description=description,
        color=PANEL_COLOR,
    )
    embed.set_footer(
        text=(
            f"Свободно {free} из {total} · кнопки: страница {page + 1}/{pages}"
            f" · обновлено {now:%H:%M} МСК"
        )
    )
    return embed


class PanelView(discord.ui.View):
    def __init__(
        self,
        service: "EfiryService",
        *,
        page: int = 0,
        grid: DayGrid | None = None,
        claims: dict[int, Claim] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.page = page
        self.grid = grid
        self.claims = claims or {}
        self._fill()

    def _fill(self) -> None:
        total = len(self.grid.slots) if self.grid is not None else 0
        pages = pages_count(total)

        for index in range(SLOTS_PER_PAGE):
            slot_index = self.page * SLOTS_PER_PAGE + index
            row = index // 5
            if self.grid is not None and slot_index < total:
                slot = self.grid.slots[slot_index]
                taken = slot_index in self.claims
                button = discord.ui.Button(
                    label=f"{slot_index + 1} · {slot.time}",
                    custom_id=f"efiry:slot:{index}",
                    row=row,
                    style=(
                        discord.ButtonStyle.danger if taken else discord.ButtonStyle.success
                    ),
                )
                button.callback = functools.partial(self._on_slot, index=index)
            else:
                button = discord.ui.Button(
                    label="—",
                    custom_id=f"efiry:slot:{index}",
                    row=row,
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                )
            self.add_item(button)

        previous = discord.ui.Button(
            label="◀",
            custom_id="efiry:prev",
            row=NAV_ROW,
            style=discord.ButtonStyle.secondary,
            disabled=total == 0 or self.page <= 0,
        )
        previous.callback = self._on_previous
        self.add_item(previous)

        page_label = discord.ui.Button(
            label=f"{self.page + 1}/{pages}",
            custom_id="efiry:page",
            row=NAV_ROW,
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        self.add_item(page_label)

        following = discord.ui.Button(
            label="▶",
            custom_id="efiry:next",
            row=NAV_ROW,
            style=discord.ButtonStyle.secondary,
            disabled=total == 0 or self.page >= pages - 1,
        )
        following.callback = self._on_next
        self.add_item(following)

        reload_button = discord.ui.Button(
            label="🔄",
            custom_id="efiry:refresh",
            row=NAV_ROW,
            style=discord.ButtonStyle.primary,
        )
        reload_button.callback = self._on_reload
        self.add_item(reload_button)

    async def _on_slot(self, interaction: discord.Interaction, index: int) -> None:
        await self.service.toggle_slot(interaction, index)

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        await self.service.page(interaction, -1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self.service.page(interaction, 1)

    async def _on_reload(self, interaction: discord.Interaction) -> None:
        await self.service.force_reload(interaction)


class EfiryService:
    def __init__(self, store: Store, cache: SheetCache, tz: ZoneInfo) -> None:
        self.store = store
        self.cache = cache
        self.tz = tz
        self._role_ids: dict[int, int] = {}

    @property
    def today(self) -> date:
        return datetime.now(self.tz).date()

    @property
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def resolve_role(self, guild: discord.Guild | None) -> discord.Role | None:
        if guild is None:
            return None
        cached_id = self._role_ids.get(guild.id)
        if cached_id is not None:
            role = guild.get_role(cached_id)
            if role is not None:
                return role
        role = guild.get_role(config.ROLE_ID) if config.ROLE_ID is not None else None
        if role is None:
            target = config.ROLE_NAME.strip().lower()
            role = next(
                (item for item in guild.roles if item.name.strip().lower() == target), None
            )
        if role is None:
            role = next(
                (item for item in guild.roles if "эфирный отдел" in item.name.lower()), None
            )
        if role is not None:
            self._role_ids[guild.id] = role.id
        return role

    async def state(self, channel_id: int) -> tuple[DayGrid, dict[int, Claim], int]:
        day = self.today
        grid = await self.cache.day(day)
        claims = self.store.claims(channel_id, day)
        panel = self.store.panel(channel_id)
        if panel is not None and panel.day == day.isoformat():
            page = clamp_page(panel.page, len(grid.slots))
        else:
            page = align_page(len(grid.slots), claims)
        return grid, claims, page

    async def render(
        self, channel_id: int, *, page: int | None = None, force: bool = False
    ) -> tuple[discord.Embed, PanelView]:
        if force:
            await self.cache.day(self.today, force=True)
        try:
            grid, claims, current = await self.state(channel_id)
        except SheetError as exc:
            embed = discord.Embed(
                title="Эфиры",
                description=f"⚠️ {exc}",
                color=discord.Color.red(),
            )
            embed.set_footer(text=f"обновлено {self.now:%H:%M} МСК")
            return embed, PanelView(self)
        target = current if page is None else clamp_page(page, len(grid.slots))
        return describe(grid, claims, target, self.now, self.tz), PanelView(
            self, page=target, grid=grid, claims=claims
        )

    async def update_message(
        self,
        message: discord.Message,
        *,
        page: int | None = None,
        force: bool = False,
        interaction: discord.Interaction | None = None,
    ) -> None:
        embed, view = await self.render(message.channel.id, page=page, force=force)
        try:
            if interaction is not None:
                await interaction.edit_original_response(embed=embed, view=view)
            else:
                await message.edit(embed=embed, view=view)
        except discord.NotFound:
            self.store.delete_panel(message.channel.id)
            return
        self.store.save_panel(message.channel.id, message.id, self.today, view.page)

    async def _resolve_message(
        self, interaction: discord.Interaction
    ) -> discord.Message | None:
        if interaction.message is not None:
            return interaction.message
        panel = self.store.panel(interaction.channel_id)
        if panel is None or interaction.channel is None:
            return None
        try:
            return await interaction.channel.fetch_message(panel.message_id)
        except discord.HTTPException:
            return None

    async def toggle_slot(self, interaction: discord.Interaction, index: int) -> None:
        await interaction.response.defer()
        message = await self._resolve_message(interaction)
        if message is None:
            await interaction.followup.send(
                "Панель эфиров не найдена — создайте её командой /панель.", ephemeral=True
            )
            return
        try:
            grid, claims, page = await self.state(interaction.channel_id)
        except SheetError as exc:
            await self.update_message(message, interaction=interaction)
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        slot_index = page * SLOTS_PER_PAGE + index
        if slot_index >= len(grid.slots):
            await self.update_message(message, interaction=interaction)
            await interaction.followup.send(
                "Этот слот уже вне сетки — нажмите 🔄, чтобы обновить.", ephemeral=True
            )
            return

        slot = grid.slots[slot_index]
        claim = claims.get(slot_index)
        user = interaction.user

        if claim is not None and claim.user_id != user.id:
            await self.update_message(message, interaction=interaction)
            await interaction.followup.send(
                f"Волна #{slot_index + 1} ({slot.time}) уже занята — {claim.user_name}.",
                ephemeral=True,
            )
            return

        if claim is None:
            self.store.add_claim(
                interaction.channel_id,
                grid.day,
                slot_index,
                user.id,
                user_label(user),
            )
            notice = f"Волна #{slot_index + 1} ({slot.time}) занята вами."
        else:
            self.store.remove_claim(interaction.channel_id, grid.day, slot_index)
            notice = f"Волна #{slot_index + 1} ({slot.time}) снова свободна."

        fresh_claims = self.store.claims(interaction.channel_id, grid.day)
        new_page = align_page(len(grid.slots), fresh_claims)
        await self.update_message(message, page=new_page, interaction=interaction)
        await interaction.followup.send(notice, ephemeral=True)

    async def page(self, interaction: discord.Interaction, delta: int) -> None:
        await interaction.response.defer()
        message = await self._resolve_message(interaction)
        if message is None:
            await interaction.followup.send(
                "Панель эфиров не найдена — создайте её командой /панель.", ephemeral=True
            )
            return
        try:
            grid, _, page = await self.state(interaction.channel_id)
        except SheetError as exc:
            await self.update_message(message, interaction=interaction)
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        target = clamp_page(page + delta, len(grid.slots))
        if target == page:
            await interaction.followup.send("Дальше страниц нет.", ephemeral=True)
            return
        await self.update_message(message, page=target, interaction=interaction)

    async def force_reload(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        message = await self._resolve_message(interaction)
        if message is None:
            await interaction.followup.send(
                "Панель эфиров не найдена — создайте её командой /панель.", ephemeral=True
            )
            return
        await self.update_message(message, force=True, interaction=interaction)
        await interaction.followup.send(
            f"Данные перечитаны из таблицы, день — {self.today:%d.%m.%Y} (МСК).",
            ephemeral=True,
        )

    async def check_reminders(self, bot: "EfiryBot") -> None:
        now = self.now
        day = now.date()
        panels = self.store.all_panels()
        if not panels:
            return
        try:
            grid = await self.cache.day(day)
        except SheetError as exc:
            log.warning("Напоминания: %s", exc)
            return

        for panel_row in panels:
            channel = bot.get_channel(panel_row.channel_id)
            if channel is None:
                continue
            claims = self.store.claims(channel.id, day)
            for index, slot in enumerate(grid.slots):
                start = slot_start(day, slot.time, self.tz)
                if start is None:
                    continue
                seconds = (start - now).total_seconds()
                if seconds <= 0 or seconds > config.REMINDER_LEAD_SECONDS:
                    continue
                if index in claims:
                    continue
                if self.store.notice_sent(channel.id, day, index):
                    continue
                await self._send_reminder(channel, index, slot, seconds)
                self.store.mark_notice(channel.id, day, index)

    async def _send_reminder(
        self, channel: discord.abc.Messageable, index: int, slot: Slot, seconds: float
    ) -> None:
        role = self.resolve_role(getattr(channel, "guild", None))
        minutes = max(1, round(seconds / 60))
        line = (
            f"`#{index + 1}` **{slot.time}** · {shorten(slot.title)}"
            f" — начало через {minutes} мин."
        )
        if role is not None:
            text = f"{role.mention} **Не взята эфирная волна!**\n{line}"
            allowed = discord.AllowedMentions(roles=[role])
        else:
            log.warning("Роль «%s» не найдена — напоминание без тега", config.ROLE_NAME)
            text = f"**Не взята эфирная волна!**\n{line}"
            allowed = discord.AllowedMentions.none()
        try:
            await channel.send(text, allowed_mentions=allowed)
        except discord.HTTPException as exc:
            log.warning("Не удалось отправить напоминание: %s", exc)

    def summary_text(
        self, day: date, grid: DayGrid | None, claims: dict[int, Claim]
    ) -> str:
        weekday = grid.weekday if grid is not None else WEEKDAYS[day.weekday()]
        header = f"📊 **Итог дня · {day:%d.%m.%Y}, {weekday}**"
        if not claims:
            return f"{header}\nСегодня ни одну волну не взяли."

        totals: dict[int, dict[str, object]] = {}
        for claim in claims.values():
            entry = totals.setdefault(claim.user_id, {"name": claim.user_name, "count": 0})
            entry["count"] = int(entry["count"]) + 1
            entry["name"] = claim.user_name

        ranking = sorted(
            totals.items(),
            key=lambda item: (-int(item[1]["count"]), str(item[1]["name"]).lower()),
        )
        best = int(ranking[0][1]["count"])
        total_taken = f"Взято волн: **{len(claims)}**"
        if grid is not None:
            total_taken += f" из {len(grid.slots)}"

        lines = [header, total_taken, ""]
        for place, (user_id, entry) in enumerate(ranking, start=1):
            count = int(entry["count"])
            top = " **(Топ дня!)**" if count == best else ""
            lines.append(
                f"`{place}.` **{count}** {plural(count, WAVE_FORMS)} — <@{user_id}>{top}"
            )
        return "\n".join(lines)

    async def check_summaries(self, bot: "EfiryBot") -> None:
        now = self.now
        if (now.hour, now.minute) < (config.SUMMARY_HOUR, config.SUMMARY_MINUTE):
            return
        day = now.date()
        for panel_row in self.store.all_panels():
            channel = bot.get_channel(panel_row.channel_id)
            if channel is None:
                continue
            if self.store.summary_sent(channel.id, day):
                continue
            claims = self.store.claims(channel.id, day)
            try:
                grid = await self.cache.day(day)
            except SheetError:
                grid = None
            try:
                await channel.send(
                    self.summary_text(day, grid, claims),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                log.warning("Не удалось отправить итог дня: %s", exc)
                continue
            self.store.mark_summary(channel.id, day)

    async def check_daily_post(self, bot: "EfiryBot") -> None:
        now = self.now
        if (now.hour, now.minute) < (config.DAILY_POST_HOUR, config.DAILY_POST_MINUTE):
            return
        day = now.date()
        for panel_row in self.store.all_panels():
            channel = bot.get_channel(panel_row.channel_id)
            if channel is None:
                continue
            if self.store.post_sent(channel.id, day):
                continue
            embed, view = await self.render(channel.id, page=0, force=True)
            try:
                message = await channel.send(embed=embed, view=view)
            except discord.HTTPException as exc:
                log.warning("Не удалось отправить новую форму: %s", exc)
                continue
            self.store.save_panel(channel.id, message.id, day, view.page)
            self.store.mark_post(channel.id, day, message.id)
            log.info("Новая форма на %s отправлена в канал %s", day, channel.id)
            if panel_row.message_id != message.id:
                await self._retire_message(channel, panel_row.message_id)

    async def _retire_message(
        self, channel: discord.abc.Messageable, message_id: int
    ) -> None:
        try:
            old = await channel.fetch_message(message_id)
        except discord.HTTPException:
            return
        embed = None
        if old.embeds:
            embed = discord.Embed.from_dict(old.embeds[0].to_dict())
            embed.set_footer(text="Устарело — актуальная форма выше.")
        try:
            await old.edit(embed=embed, view=None)
        except discord.HTTPException as exc:
            log.warning("Не удалось закрыть прошлую форму: %s", exc)


class EfiryBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.tz = ZoneInfo(config.TIMEZONE)
        self.store = Store(config.DB_PATH)
        self.cache = SheetCache(
            config.SHEET_ID, config.SHEET_GID, config.SHEET_NAME, ttl=config.CACHE_TTL
        )
        self.service = EfiryService(self.store, self.cache, self.tz)
        self.audit_log = AuditLog(config.DATA_DIR / "audit.csv", self.tz)
        self.apps = ApplicationsService(self.store, self.tz, self.audit_log)
        self._ready_done = False
        self._ticker = tasks.loop(minutes=config.TICKER_MINUTES)(self._tick)
        self._reminder_loop = tasks.loop(minutes=config.REMINDER_MINUTES)(self._check_reminders)
        self._summary_loop = tasks.loop(minutes=1)(self._check_summaries)
        self._daily_post_loop = tasks.loop(minutes=1)(self._daily_post)

    async def setup_hook(self) -> None:
        self.add_view(PanelView(self.service))
        self.add_view(ApplyPanelView(self.apps))
        self.add_view(ReviewView(self.apps))
        self.tree.on_error = self._on_app_error
        self.tree.add_command(self._panel_command())
        self.tree.add_command(self._sync_command())
        self.tree.add_command(self._summary_command())
        self.tree.add_command(self._reset_command())
        self.tree.add_command(self._applications_command())

    async def _on_app_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            text = "Недостаточно прав: команда требует права «Управление сообщениями»."
        else:
            log.error("Ошибка команды: %s", error)
            text = "Не удалось выполнить команду — подробности в логе бота."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    def _panel_command(self) -> app_commands.Command:
        bot = self

        @app_commands.command(name="панель", description="Показать сетку эфиров на сегодня")
        async def panel(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            existing = bot.store.panel(interaction.channel_id)
            message: discord.Message | None = None
            if existing is not None and interaction.channel is not None:
                try:
                    message = await interaction.channel.fetch_message(existing.message_id)
                except discord.HTTPException:
                    message = None
            if message is not None:
                await bot.service.update_message(message, force=True)
                await interaction.followup.send(
                    "Панель уже есть в канале — обновил её.", ephemeral=True
                )
                return
            embed, view = await bot.service.render(interaction.channel_id)
            message = await interaction.followup.send(embed=embed, view=view, wait=True)
            bot.store.save_panel(
                interaction.channel_id, message.id, bot.service.today, view.page
            )

        return panel

    def _sync_command(self) -> app_commands.Command:
        bot = self

        @app_commands.command(name="обновить", description="Перечитать таблицу и обновить панель")
        async def refresh(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            panel_row = bot.store.panel(interaction.channel_id)
            if panel_row is None or interaction.channel is None:
                await interaction.followup.send(
                    "В этом канале нет панели — создайте её командой /панель.",
                    ephemeral=True,
                )
                return
            try:
                message = await interaction.channel.fetch_message(panel_row.message_id)
            except discord.HTTPException:
                bot.store.delete_panel(interaction.channel_id)
                await interaction.followup.send(
                    "Сообщение панели пропало — создайте её заново командой /панель.",
                    ephemeral=True,
                )
                return
            await bot.service.update_message(message, force=True)
            await interaction.followup.send("Панель обновлена.", ephemeral=True)

        return refresh

    def _summary_command(self) -> app_commands.Command:
        bot = self

        @app_commands.command(name="итог", description="Итог дня: кто сколько волн взял")
        async def summary(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            day = bot.service.today
            claims = bot.store.claims(interaction.channel_id, day)
            try:
                grid = await bot.cache.day(day)
            except SheetError:
                grid = None
            await interaction.followup.send(
                bot.service.summary_text(day, grid, claims), ephemeral=True
            )

        return summary

    def _reset_command(self) -> app_commands.Command:
        bot = self

        @app_commands.command(
            name="сброс", description="Снять все отметки занятости на сегодня"
        )
        @app_commands.checks.has_permissions(manage_messages=True)
        async def reset(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            removed = bot.store.clear_day(interaction.channel_id, bot.service.today)
            panel_row = bot.store.panel(interaction.channel_id)
            if panel_row is not None and interaction.channel is not None:
                try:
                    message = await interaction.channel.fetch_message(panel_row.message_id)
                except discord.HTTPException:
                    message = None
                if message is not None:
                    await bot.service.update_message(message, page=0)
            await interaction.followup.send(f"Снято отметок: {removed}.", ephemeral=True)

        return reset

    def _applications_command(self) -> app_commands.Command:
        bot = self

        @app_commands.command(
            name="заявки",
            description="Опубликовать или обновить панель заявок на роли",
        )
        @app_commands.checks.has_permissions(manage_guild=True)
        async def applications(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            await bot.apps.ensure_panel(bot)
            await interaction.followup.send(
                "Панель заявок опубликована или обновлена.", ephemeral=True
            )

        return applications

    async def _tick(self) -> None:
        await self.wait_until_ready()
        self.store.purge_before(self.service.today)
        for panel_row in self.store.all_panels():
            channel = self.get_channel(panel_row.channel_id)
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(panel_row.message_id)
            except discord.NotFound:
                self.store.delete_panel(panel_row.channel_id)
                continue
            except discord.HTTPException:
                continue
            try:
                await self.service.update_message(message)
            except SheetError as exc:
                log.warning("Не удалось обновить панель %s: %s", panel_row.channel_id, exc)
            except discord.HTTPException as exc:
                log.warning("Discord отклонил обновление панели: %s", exc)

    async def _check_reminders(self) -> None:
        await self.wait_until_ready()
        try:
            await self.service.check_reminders(self)
        except Exception:
            log.exception("Ошибка цикла напоминаний")

    async def _check_summaries(self) -> None:
        await self.wait_until_ready()
        try:
            await self.service.check_summaries(self)
        except Exception:
            log.exception("Ошибка цикла итогов дня")

    async def _daily_post(self) -> None:
        await self.wait_until_ready()
        try:
            await self.service.check_daily_post(self)
        except Exception:
            log.exception("Ошибка публикации новой формы")

    async def on_ready(self) -> None:
        if self._ready_done:
            return
        self._ready_done = True
        try:
            if config.GUILD_ID is not None:
                guild = discord.Object(id=config.GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
            log.info("Слэш-команды синхронизированы")
        except discord.HTTPException as exc:
            log.warning("Не удалось синхронизировать команды: %s", exc)

        if config.PANEL_CHANNEL_ID is not None:
            await self._ensure_panel_channel()

        await self.apps.ensure_panel(self)

        if not self._ticker.is_running():
            self._ticker.start()
        if not self._reminder_loop.is_running():
            self._reminder_loop.start()
        if not self._summary_loop.is_running():
            self._summary_loop.start()
        if not self._daily_post_loop.is_running():
            self._daily_post_loop.start()

        log.info(
            "Бот запущен как %s, часовой пояс %s, новая форма в %02d:%02d, итог дня в %02d:%02d",
            self.user,
            config.TIMEZONE,
            config.DAILY_POST_HOUR,
            config.DAILY_POST_MINUTE,
            config.SUMMARY_HOUR,
            config.SUMMARY_MINUTE,
        )

    async def _ensure_panel_channel(self) -> None:
        channel = self.get_channel(config.PANEL_CHANNEL_ID)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("Канал панели %s недоступен", config.PANEL_CHANNEL_ID)
            return
        panel_row = self.store.panel(channel.id)
        if panel_row is not None:
            try:
                message = await channel.fetch_message(panel_row.message_id)
                await self.service.update_message(message, force=True)
                return
            except discord.HTTPException:
                self.store.delete_panel(channel.id)
        embed, view = await self.service.render(channel.id)
        message = await channel.send(embed=embed, view=view)
        self.store.save_panel(channel.id, message.id, self.service.today, view.page)

    async def close(self) -> None:
        for loop in (
            self._ticker,
            self._reminder_loop,
            self._summary_loop,
            self._daily_post_loop,
        ):
            if loop.is_running():
                loop.cancel()
        await self.cache.close()
        self.store.close()
        await super().close()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%d.%m %H:%M:%S",
    )
    if not config.DISCORD_TOKEN:
        raise SystemExit(
            "Не задан DISCORD_TOKEN. Скопируйте .env.example в .env и вставьте токен бота."
        )
    bot = EfiryBot()
    try:
        await bot.start(config.DISCORD_TOKEN)
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
