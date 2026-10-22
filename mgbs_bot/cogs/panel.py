from __future__ import annotations

import asyncio

import disnake
from disnake.ext import commands

from mgbs_bot.config import BANNER_PATH, PANEL_VERSION
from mgbs_bot.ui.embeds import make_panel_embed
from mgbs_bot.ui.panel import PersistentTaskPanelView
from mgbs_bot.utils import ALLOWED_MENTIONS, logger, make_banner_file, store


class PanelCog(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self._panel_locks: dict[int, asyncio.Lock] = {}
        self._startup_panel_check_done = False
        self._persistent_view_registered = False

    def _panel_message_options(self, *, include_banner: bool) -> dict:
        options = {
            "embed": make_panel_embed(include_banner=include_banner),
            "view": PersistentTaskPanelView(self.bot),
            "allowed_mentions": ALLOWED_MENTIONS,
        }
        banner = make_banner_file() if include_banner else None
        if banner is not None:
            options["file"] = banner
        return options

    def _channel_permission_gaps(
        self, channel: disnake.TextChannel, action: str
    ) -> list[str]:
        diagnostic = getattr(self.bot, "log_permission_diagnostics", None)
        if callable(diagnostic):
            return diagnostic(
                channel.guild,
                action=action,
                channel=channel,
                required=("view_channel", "send_messages", "embed_links"),
            )
        return []

    async def _delete_queued_panels(self, guild: disnake.Guild) -> bool:
        all_deleted = True
        active = store.get_panel(guild.id)
        for queued in store.get_panel_cleanup(guild.id):
            if active and (
                active["channel_id"] == queued["channel_id"]
                and active["message_id"] == queued["message_id"]
            ):
                logger.error(
                    "Skipped panel cleanup pointing at the active message: guild_id=%s message_id=%s",
                    guild.id,
                    queued["message_id"],
                )
                all_deleted = False
                continue

            channel = self.bot.get_channel(queued["channel_id"])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(queued["channel_id"])
                except disnake.NotFound:
                    logger.warning(
                        "Old task-panel channel no longer exists: guild_id=%s channel_id=%s",
                        guild.id,
                        queued["channel_id"],
                    )
                    store.clear_panel_cleanup(
                        guild.id, queued["channel_id"], queued["message_id"]
                    )
                    continue
                except disnake.HTTPException:
                    logger.exception(
                        "Could not fetch old task-panel channel: channel_id=%s",
                        queued["channel_id"],
                    )
                    all_deleted = False
                    continue

            if not isinstance(channel, disnake.TextChannel) or channel.guild.id != guild.id:
                logger.error(
                    "Old task-panel channel is unavailable: channel_id=%s type=%s",
                    queued["channel_id"],
                    type(channel).__name__,
                )
                all_deleted = False
                continue

            try:
                await channel.get_partial_message(queued["message_id"]).delete()
            except disnake.NotFound:
                logger.info(
                    "Old task panel was already absent: message_id=%s", queued["message_id"]
                )
            except disnake.HTTPException:
                logger.exception(
                    "Could not delete old task panel: channel_id=%s message_id=%s",
                    queued["channel_id"],
                    queued["message_id"],
                )
                all_deleted = False
                continue
            store.clear_panel_cleanup(guild.id, queued["channel_id"], queued["message_id"])
            logger.info(
                "Deleted previous task panel: guild_id=%s message_id=%s",
                guild.id,
                queued["message_id"],
            )
        return all_deleted

    async def _publish_replacement(
        self,
        guild: disnake.Guild,
        channel: disnake.TextChannel,
        *,
        action: str,
    ) -> disnake.Message:
        missing = self._channel_permission_gaps(channel, action)
        if missing:
            raise PermissionError(
                "Missing channel permissions for task panel: " + ", ".join(missing)
            )

        bot_member = guild.me
        if bot_member is None and self.bot.user is not None:
            bot_member = guild.get_member(self.bot.user.id)
        include_banner = BANNER_PATH.is_file() and bot_member is not None
        if include_banner:
            include_banner = channel.permissions_for(bot_member).attach_files
        if BANNER_PATH.is_file() and not include_banner:
            logger.warning(
                "Publishing task panel without banner: guild_id=%s", guild.id
            )

        message = await channel.send(**self._panel_message_options(include_banner=include_banner))
        try:
            store.rotate_panel(guild.id, channel.id, message.id, PANEL_VERSION)
        except Exception:
            persisted = store.get_panel(guild.id)
            if persisted != {
                "channel_id": channel.id,
                "message_id": message.id,
                "version": PANEL_VERSION,
            }:
                try:
                    await message.delete()
                except disnake.HTTPException:
                    logger.exception(
                        "Could not remove an untracked task panel: message_id=%s", message.id
                    )
            raise

        logger.info(
            "Task panel published: action=%s guild_id=%s channel_id=%s message_id=%s",
            action,
            guild.id,
            channel.id,
            message.id,
        )
        await self._delete_queued_panels(guild)
        return message

    async def _resolve_panel_channel(
        self, guild: disnake.Guild, channel_id: int
    ) -> disnake.TextChannel | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except disnake.HTTPException:
                logger.exception(
                    "Could not fetch task-panel channel: guild_id=%s channel_id=%s",
                    guild.id,
                    channel_id,
                )
                return None
        if not isinstance(channel, disnake.TextChannel) or channel.guild.id != guild.id:
            logger.error(
                "Task-panel channel is unavailable: guild_id=%s channel_id=%s type=%s",
                guild.id,
                channel_id,
                type(channel).__name__,
            )
            return None
        return channel

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._persistent_view_registered:
            self.bot.add_view(PersistentTaskPanelView(self.bot))
            self._persistent_view_registered = True

        if self._startup_panel_check_done:
            return
        retry_needed = False
        for guild in self.bot.guilds:
            lock = self._panel_locks.setdefault(guild.id, asyncio.Lock())
            async with lock:
                saved = store.get_panel(guild.id)
                if saved is None:
                    continue
                channel = await self._resolve_panel_channel(guild, saved["channel_id"])
                if channel is None:
                    retry_needed = True
                    continue

                needs_replace = saved.get("version", 1) < PANEL_VERSION
                if not needs_replace:
                    try:
                        await channel.fetch_message(saved["message_id"])
                    except disnake.NotFound:
                        logger.warning(
                            "Saved task panel message was deleted; republishing: guild_id=%s message_id=%s",
                            guild.id,
                            saved["message_id"],
                        )
                        needs_replace = True
                    except disnake.HTTPException:
                        logger.exception(
                            "Could not verify saved task panel: message_id=%s",
                            saved["message_id"],
                        )
                        retry_needed = True
                        continue

                if needs_replace:
                    try:
                        await self._publish_replacement(
                            guild,
                            channel,
                            action="replace outdated task panel on startup",
                        )
                    except Exception:
                        logger.exception(
                            "Could not refresh task panel on startup: guild_id=%s message_id=%s",
                            guild.id,
                            saved["message_id"],
                        )
                        retry_needed = True
                        continue
                else:
                    logger.info(
                        "Existing task panel kept as-is: guild_id=%s message_id=%s",
                        guild.id,
                        saved["message_id"],
                    )
                if store.get_panel_cleanup(guild.id):
                    retry_needed = True
        self._startup_panel_check_done = not retry_needed

    @commands.slash_command(name="panel", description="Опубликовать панель заданий в канале")
    async def panel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel,
    ) -> None:
        if inter.guild is None or not isinstance(inter.author, disnake.Member):
            await inter.response.send_message(
                "Команда доступна только на сервере.", ephemeral=True
            )
            return
        if not inter.author.guild_permissions.administrator:
            await inter.response.send_message(
                "Команда доступна только администраторам.", ephemeral=True
            )
            return
        if channel.guild.id != inter.guild.id:
            await inter.response.send_message(
                "Выберите текстовый канал этого сервера.", ephemeral=True
            )
            return

        missing = self._channel_permission_gaps(channel, "publish new task panel")
        if missing:
            await inter.response.send_message(
                "У бота не хватает прав в выбранном канале: " + ", ".join(missing) + ".",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True)
        lock = self._panel_locks.setdefault(inter.guild.id, asyncio.Lock())
        async with lock:
            try:
                await self._publish_replacement(
                    inter.guild,
                    channel,
                    action="publish new task panel from command",
                )
            except Exception as exc:
                logger.exception(
                    "Could not create task panel: guild_id=%s channel_id=%s exception=%s",
                    inter.guild.id,
                    channel.id,
                    type(exc).__name__,
                )
                if isinstance(exc, PermissionError):
                    error_text = str(exc)
                elif isinstance(exc, disnake.HTTPException) and exc.code == 50013:
                    error_text = (
                        "Discord отказал из-за прав канала. Проверьте отправку сообщений, "
                        "встраивание ссылок и прикрепление файлов для роли бота."
                    )
                else:
                    error_text = "Проверьте логи `data/logs/bot.log` и повторите попытку."
                await inter.edit_original_response(
                    content=f"Не удалось опубликовать панель. {error_text}"
                )
                return

        if store.get_panel_cleanup(inter.guild.id):
            await inter.edit_original_response(
                content=f"Новая панель опубликована в {channel.mention}. "
                "Удаление старого сообщения будет повторено автоматически."
            )
        else:
            await inter.edit_original_response(
                content=f"Новая панель опубликована в {channel.mention}."
            )
