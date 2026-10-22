from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import disnake
from disnake.ext import commands

from mgbs_bot.config import (
    AFK_HOURS,
    AFK_PING_CHANNEL_ID,
    AFK_REMINDER_COOLDOWN_HOURS,
    AFK_SCAN_INTERVAL_MINUTES,
    EMBED_COLOR,
)
from mgbs_bot.utils import (
    ALLOWED_MENTIONS,
    eligible_task_roles,
    footer_text,
    local_now,
    logger,
    member_open_tasks,
    store,
    task_roles_text,
)

ROLE_MENTION = disnake.AllowedMentions(roles=True, users=True, everyone=False)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class AfkReminderCog(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self._scan_task: asyncio.Task | None = None
        self._scan_lock = asyncio.Lock()
        self._last_write: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        if not AFK_PING_CHANNEL_ID:
            logger.warning(
                "AFK_PING_CHANNEL_ID is not set: members will only get DM reminders"
            )
        self._scan_task = asyncio.create_task(
            self._scan_loop(), name="afk-reminder-scan"
        )

    async def cog_unload(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
            self._scan_task = None

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        self._touch_activity(message.guild.id, message.author.id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: disnake.Member,
        before: disnake.VoiceState,
        after: disnake.VoiceState,
    ) -> None:
        if member.bot or member.guild is None:
            return
        if after.channel is not None and (
            before.channel != after.channel or before.self_mute != after.self_mute
        ):
            self._touch_activity(member.guild.id, member.id)

    def _touch_activity(self, guild_id: int, user_id: int) -> None:
        key = (guild_id, user_id)
        now = time.monotonic()
        if now - self._last_write.get(key, 0.0) < 60.0:
            return
        self._last_write[key] = now
        try:
            store.touch_member_activity(guild_id, user_id, local_now().isoformat())
        except Exception:
            logger.exception(
                "Could not persist member activity: guild_id=%s user_id=%s",
                guild_id,
                user_id,
            )

    async def _scan_loop(self) -> None:
        await self.bot.wait_until_ready()
        interval = max(60.0, AFK_SCAN_INTERVAL_MINUTES * 60.0)
        while not self.bot.is_closed():
            async with self._scan_lock:
                try:
                    await self._scan_all_guilds()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("AFK reminder scan failed")
            await asyncio.sleep(interval)

    async def _scan_all_guilds(self) -> None:
        now = local_now()
        afk_cutoff = now - timedelta(hours=AFK_HOURS)
        cooldown_cutoff = now - timedelta(hours=AFK_REMINDER_COOLDOWN_HOURS)

        for guild in self.bot.guilds:
            tasks = store.list_tasks(guild.id)
            if not tasks:
                continue
            activity = store.list_member_activity(guild.id)
            ping_channel = self._resolve_ping_channel(guild)

            afk_members = []
            for member in guild.members:
                if member.bot:
                    continue
                last_active = _parse_timestamp(activity.get(member.id) or "")
                if last_active is None:
                    store.touch_member_activity(guild.id, member.id, now.isoformat())
                    continue
                if last_active >= afk_cutoff:
                    continue
                matching = member_open_tasks(tasks, member)
                if matching:
                    afk_members.append((member, matching))

            for member, matching in afk_members:
                await self._ping_member(
                    guild, ping_channel, member, matching, now, cooldown_cutoff
                )

    def _resolve_ping_channel(self, guild: disnake.Guild) -> disnake.TextChannel | None:
        if not AFK_PING_CHANNEL_ID:
            return None
        channel = self.bot.get_channel(AFK_PING_CHANNEL_ID)
        if isinstance(channel, disnake.TextChannel) and channel.guild.id == guild.id:
            return channel
        return None

    async def _ping_member(
        self,
        guild: disnake.Guild,
        channel: disnake.TextChannel | None,
        member: disnake.Member,
        matching_tasks: list[dict],
        now: datetime,
        cooldown_cutoff: datetime,
    ) -> None:
        fresh_tasks = []
        eligible = eligible_task_roles(member)
        for task in matching_tasks:
            task_roles = [role_id for role_id in task["role_ids"] if role_id in eligible]
            if not task_roles:
                continue
            last_ping = _parse_timestamp(
                store.get_last_task_ping(guild.id, task["id"], member.id) or ""
            )
            if last_ping is None or last_ping < cooldown_cutoff:
                fresh_tasks.append((task, task_roles))
        if not fresh_tasks:
            return

        role_mentions = sorted(
            {role_id for _, role_ids in fresh_tasks for role_id in role_ids}
        )
        role_line = " ".join(f"<@&{role_id}>" for role_id in role_mentions)
        lines = [
            f"``#{task['id']}`` **{task['title']}**\n"
            f"Роли: {task_roles_text(role_ids)}\n"
            f"Дедлайн: {task['deadline']}"
            for task, role_ids in fresh_tasks
        ]

        embed = disnake.Embed(
            title="💤 Напоминание о заданиях",
            description=(
                f"{member.mention}, вы не были активны уже **{int(AFK_HOURS)} ч.**, "
                f"но для ваших ролей есть открытые задания:\n\n" + "\n\n".join(lines)
            ),
            color=EMBED_COLOR,
        )
        embed.set_footer(
            text=footer_text(member.display_name), icon_url=member.display_avatar.url
        )

        if channel is not None:
            try:
                await channel.send(
                    content=f"{role_line} — {member.mention}".strip(),
                    embed=embed,
                    allowed_mentions=ROLE_MENTION,
                )
            except disnake.HTTPException:
                logger.exception(
                    "Could not send AFK reminder to chat: guild_id=%s channel_id=%s user_id=%s",
                    guild.id,
                    channel.id,
                    member.id,
                )

        dm_embed = disnake.Embed(
            title="💤 Задания ждут вас",
            description="Для ваших ролей есть открытые задания:\n\n" + "\n\n".join(lines),
            color=EMBED_COLOR,
        )
        dm_embed.set_footer(
            text=footer_text(member.display_name), icon_url=member.display_avatar.url
        )
        try:
            await member.send(embed=dm_embed, allowed_mentions=ALLOWED_MENTIONS)
        except disnake.Forbidden:
            logger.info(
                "Member has DMs closed; skipped AFK DM: guild_id=%s user_id=%s",
                guild.id,
                member.id,
            )
        except disnake.HTTPException:
            logger.exception(
                "Could not send AFK reminder DM: guild_id=%s user_id=%s",
                guild.id,
                member.id,
            )

        stamp = now.isoformat()
        for task, _role_ids in fresh_tasks:
            store.record_task_ping(guild.id, task["id"], member.id, stamp)
        logger.info(
            "AFK reminder sent: guild_id=%s user_id=%s task_ids=%s",
            guild.id,
            member.id,
            [task["id"] for task, _ in fresh_tasks],
        )
