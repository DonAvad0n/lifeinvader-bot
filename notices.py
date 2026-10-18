from __future__ import annotations

import asyncio
import logging

import discord

import config

log = logging.getLogger("lifeinvader.notices")

_tasks: set[asyncio.Task] = set()


async def _delete_later(
    message: discord.Message | discord.WebhookMessage, delay: int
) -> None:
    try:
        await asyncio.sleep(delay)
        await message.delete()
    except discord.NotFound:
        pass
    except discord.HTTPException as exc:
        log.debug("Не удалось удалить служебное сообщение: %s", exc)
    except asyncio.CancelledError:
        raise


async def reply(
    interaction: discord.Interaction,
    text: str | None = None,
    *,
    delay: int | None = None,
    ephemeral: bool = True,
    **kwargs: object,
) -> discord.WebhookMessage | None:
    try:
        message = await interaction.followup.send(
            text, ephemeral=ephemeral, wait=True, **kwargs
        )
    except discord.HTTPException as exc:
        log.warning("Не удалось отправить служебное сообщение: %s", exc)
        return None
    if message is None:
        return None
    timeout = config.EPHEMERAL_DELETE_SECONDS if delay is None else delay
    if timeout and timeout > 0:
        task = asyncio.create_task(_delete_later(message, timeout))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return message
