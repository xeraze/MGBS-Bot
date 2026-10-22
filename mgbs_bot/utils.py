from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import disnake
from disnake.ext import commands

from mgbs_bot.config import (
    BANNER_PATH,
    BUILDER_ROLES,
    DATABASE_PATH,
    EMBED_COLOR,
    MODELLER_ROLES,
    ROLE_NAMES,
    SCRIPTER_ROLES,
    SPECIALIST_ROLES,
    SPECIAL_TASK_APPROVER_ROLE_IDS,
    TASK_CREATOR_ROLE_IDS,
    TASK_LOG_CHANNEL_ID,
    TIMEZONE,
)
from mgbs_bot.database import TaskStore

logger = logging.getLogger("task_bot")
store = TaskStore(DATABASE_PATH)
LOCAL_TZ = ZoneInfo(TIMEZONE)
ALLOWED_MENTIONS = disnake.AllowedMentions.none()

_president_role_id = os.getenv("DISCORD_PRESIDENT_ROLE_ID", "").strip()
PRESIDENT_ROLE_ID = int(_president_role_id) if _president_role_id.isdecimal() else None


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def footer_text(username: str) -> str:
    return f"Запрос от {username} • {local_now():%d.%m.%Y, %H:%M}"


def make_banner_file() -> disnake.File | None:
    if BANNER_PATH.is_file():
        return disnake.File(BANNER_PATH, filename="banner.png")
    return None


def add_banner(embed: disnake.Embed) -> disnake.File | None:
    banner = make_banner_file()
    if banner is not None:
        embed.set_image(url="attachment://banner.png")
    return banner


def held_profile_roles(member: disnake.Member) -> list[int]:
    return [role.id for role in member.roles if role.id in ROLE_NAMES]


def eligible_task_roles(member: disnake.Member) -> set[int]:
    held = {role.id for role in member.roles}
    allowed: set[int] = set()
    for hierarchy in (BUILDER_ROLES, MODELLER_ROLES, SCRIPTER_ROLES):
        for index, role_id in enumerate(hierarchy):
            if role_id in held:
                allowed.update(hierarchy[index:])
    allowed.update(held.intersection(SPECIALIST_ROLES))
    return allowed


def member_open_tasks(tasks: list[dict], member: disnake.Member) -> list[dict]:
    eligible = eligible_task_roles(member)
    return [task for task in tasks if eligible.intersection(task["role_ids"])]


def member_is_task_creator(member: disnake.Member) -> bool:
    held = {role.id for role in member.roles}
    return member.guild_permissions.administrator or bool(held & TASK_CREATOR_ROLE_IDS)


def format_tenure(joined_at: datetime | None) -> str:
    if joined_at is None:
        return "неизвестно"
    days = max(0, (datetime.now(timezone.utc) - joined_at.astimezone(timezone.utc)).days)
    years, remainder = divmod(days, 365)
    months, days = divmod(remainder, 30)
    parts = []
    if years:
        parts.append(f"{years} г.")
    if months:
        parts.append(f"{months} мес.")
    parts.append(f"{days} дн.")
    return " ".join(parts)


def task_roles_text(role_ids: list[int]) -> str:
    return ", ".join(ROLE_NAMES.get(role_id, f"Роль {role_id}") for role_id in role_ids)


def code_block(value: object, max_length: int = 1000) -> str:
    text = str("—" if value is None or value == "" else value).replace("```", "ˋˋˋ").strip()
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return f"```{text}```"


def application_approver_role_ids(task_role_ids: list[int]) -> set[int]:
    requested = set(task_role_ids)
    approvers: set[int] = set()
    if requested.intersection((*BUILDER_ROLES, *MODELLER_ROLES)):
        approvers.add(BUILDER_ROLES[0])
    if requested.intersection(SCRIPTER_ROLES):
        approvers.add(SCRIPTER_ROLES[0])
    if requested.intersection(SPECIALIST_ROLES):
        approvers.update({BUILDER_ROLES[0], SCRIPTER_ROLES[0], *SPECIAL_TASK_APPROVER_ROLE_IDS})
    return approvers


def task_leader_role_ids(task_role_ids: list[int]) -> set[int]:
    return application_approver_role_ids(task_role_ids)


def task_progression_track(role_ids: list[int], participant_role_ids: list[int]) -> str | None:
    task_roles = set(role_ids)
    participant_roles = set(participant_role_ids)
    for track, hierarchy in (
        ("builder", BUILDER_ROLES),
        ("modeller", MODELLER_ROLES),
        ("scripter", SCRIPTER_ROLES),
    ):
        if task_roles.intersection(hierarchy):
            held_indices = [
                index for index, role_id in enumerate(hierarchy[1:], start=1)
                if role_id in participant_roles
            ]
            if held_indices:
                return f"{track}:{min(held_indices)}"
    return None


def task_welcome_embed(task: dict, participant_count: int, username: str) -> disnake.Embed:
    president = f"<@&{PRESIDENT_ROLE_ID}>" if PRESIDENT_ROLE_ID else "@президент MGBS"
    embed = disnake.Embed(
        title=f"Добро пожаловать в чат «{task['title']}»",
        description=(
            "Вы выбрали работать в команде над этим заданием.\n\n"
            f"За вашей работой будет следить {president}."
        ),
        color=EMBED_COLOR,
    )
    embed.add_field(name="🆔 Номер задания", value=code_block(f"#{task['id']}", 80), inline=False)
    embed.add_field(name="⏳ Дедлайн", value=code_block(task["deadline"], 200), inline=False)
    embed.add_field(name="👥 Участники", value=code_block(f"{participant_count} чел."), inline=False)
    embed.add_field(name="⚙️ Сложность", value=code_block(task["difficulty"], 100), inline=False)
    embed.add_field(name="📝 Задание", value=code_block(task["description"]), inline=False)
    embed.set_footer(text=footer_text(username))
    if BANNER_PATH.is_file():
        embed.set_image(url="attachment://banner.png")
    return embed


async def send_task_log(
    bot: commands.InteractionBot,
    guild: disnake.Guild,
    *,
    title: str,
    description: str,
    actor: str,
    fields: list[tuple[str, str]] | None = None,
) -> bool:
    channel = bot.get_channel(TASK_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(TASK_LOG_CHANNEL_ID)
        except disnake.HTTPException:
            logger.exception("Could not fetch the task log channel")
            return False
    if not isinstance(channel, disnake.TextChannel) or channel.guild.id != guild.id:
        logger.error("Task log channel %s is unavailable in guild %s", TASK_LOG_CHANNEL_ID, guild.id)
        return False

    embed = disnake.Embed(title=title, description=description, color=EMBED_COLOR)
    for field_name, field_value in fields or []:
        embed.add_field(name=field_name, value=code_block(field_value), inline=False)
    embed.set_footer(text=footer_text(actor))
    banner = add_banner(embed)
    options: dict = {"embed": embed, "allowed_mentions": ALLOWED_MENTIONS}
    if banner is not None:
        options["file"] = banner
    try:
        await channel.send(**options)
        return True
    except disnake.HTTPException:
        logger.exception("Could not write to the task log channel")
        return False
