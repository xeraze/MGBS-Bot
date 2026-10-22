from __future__ import annotations

from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from mgbs_bot.config import APPLICATIONS_CHANNEL_ID, EMBED_COLOR
from mgbs_bot.ui.tasks import ApplicationDecisionView
from mgbs_bot.utils import (
    ALLOWED_MENTIONS,
    add_banner,
    code_block,
    eligible_task_roles,
    footer_text,
    format_tenure,
    held_profile_roles,
    local_now,
    logger,
    send_task_log,
    store,
    task_roles_text,
)

if TYPE_CHECKING:
    from mgbs_bot.ui.panel import TaskBrowserView


async def submit_application(
    bot: commands.InteractionBot,
    inter: disnake.MessageInteraction | disnake.ModalInteraction,
    task: dict,
    *,
    browser: TaskBrowserView | None = None,
) -> None:
    member = inter.author
    if not isinstance(member, disnake.Member) or inter.guild is None:
        await inter.response.send_message(
            "Не удалось получить роли участника сервера.", ephemeral=True
        )
        return

    eligible = eligible_task_roles(member)
    if not eligible.intersection(task["role_ids"]):
        await inter.response.send_message(
            f"У вас нет подходящей роли для этого задания.\n"
            f"**Нужные роли:** {task_roles_text(task['role_ids'])}",
            ephemeral=True,
            allowed_mentions=ALLOWED_MENTIONS,
        )
        return

    actual_roles = held_profile_roles(member)
    inserted = store.create_application(
        task_id=task["id"],
        guild_id=inter.guild_id,
        user_id=member.id,
        username=member.display_name,
        role_ids=actual_roles,
        joined_at=member.joined_at.isoformat() if member.joined_at else None,
        submitted_at=local_now().isoformat(),
    )
    if not inserted:
        await inter.response.send_message(
            "Вы уже отправляли заявку на это задание.", ephemeral=True
        )
        return

    profile = task_roles_text(actual_roles) if actual_roles else "Профильная роль не найдена"
    try:
        if browser is not None:
            await inter.response.defer(with_message=False)
        elif isinstance(inter, disnake.ModalInteraction):
            await inter.response.defer(ephemeral=True)
        else:
            await inter.response.defer(with_message=True, ephemeral=True)
    except disnake.HTTPException:
        logger.exception("Could not defer application response for task #%s", task["id"])
        return

    try:
        channel = bot.get_channel(APPLICATIONS_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(APPLICATIONS_CHANNEL_ID)
        if not isinstance(channel, disnake.TextChannel) or channel.guild.id != inter.guild_id:
            raise RuntimeError(
                "Канал заявок не найден на этом сервере или имеет неподходящий тип."
            )

        embed = disnake.Embed(
            title="Новый отклик",
            description=f"Отклик на задание | ``{member.display_name}`` | ``{member.id}``",
            color=EMBED_COLOR,
        )
        embed.add_field(
            name="👤 Участник",
            value=code_block(f"{member.display_name}\nID {member.id}", 200),
            inline=False,
        )
        embed.add_field(name="🎭 Роль участника", value=code_block(profile, 950), inline=False)
        embed.add_field(
            name="🕒 На сервере",
            value=code_block(format_tenure(member.joined_at), 100),
            inline=False,
        )
        embed.add_field(
            name="🆔 Номер задания", value=code_block(f"#{task['id']}", 80), inline=False
        )
        embed.add_field(
            name="🎯 Роли задания",
            value=code_block(task_roles_text(task["role_ids"]), 950),
            inline=False,
        )
        embed.add_field(name="⏳ Дедлайн", value=code_block(task["deadline"], 200), inline=False)
        embed.add_field(
            name="📝 Что нужно сделать",
            value=code_block(task["description"], 950),
            inline=False,
        )
        embed.set_footer(
            text=footer_text(member.display_name), icon_url=member.display_avatar.url
        )
        banner = add_banner(embed)
        options: dict = {
            "embed": embed,
            "allowed_mentions": ALLOWED_MENTIONS,
            "view": ApplicationDecisionView(bot),
        }
        if banner is not None:
            options["file"] = banner
        message = await channel.send(**options)
        store.set_application_message(task["id"], member.id, message.id)
    except Exception:
        store.delete_application(task["id"], member.id)
        logger.exception("Could not post an application for task #%s", task["id"])
        error_text = (
            "Не удалось отправить заявку в канал заявок. "
            "Проверьте настройки канала и права бота."
        )
        if browser is not None:
            await inter.followup.send(error_text, ephemeral=True)
        else:
            await inter.edit_original_response(content=error_text)
        return

    if browser is not None:
        browser.mark_applied(task["id"])
        await inter.edit_original_response(**browser.message_kwargs())
    else:
        await inter.edit_original_response(
            content=f"Заявка на задание **#{task['id']}** отправлена.",
            allowed_mentions=ALLOWED_MENTIONS,
        )
    await send_task_log(
        bot,
        inter.guild,
        title="Новая заявка",
        description=f"{member.mention} подал заявку на задание ``#{task['id']}``.",
        actor=member.display_name,
        fields=[
            ("Роль участника", profile),
            ("На сервере", format_tenure(member.joined_at)),
            ("Задание", task["title"]),
        ],
    )
