from __future__ import annotations

import asyncio

import disnake
from disnake.ext import commands

from mgbs_bot.cogs.afk import AfkReminderCog
from mgbs_bot.cogs.panel import PanelCog
from mgbs_bot.cogs.tasks import TaskCog
from mgbs_bot.config import (
    APPLICATIONS_CHANNEL_ID,
    BANNER_PATH,
    BUILDER_ROLES,
    EMBED_COLOR,
    MODELLER_ROLES,
    ROLE_NAMES,
    SCRIPTER_ROLES,
    TASK_LOG_CHANNEL_ID,
)
from mgbs_bot.ui.tasks import (
    ApplicationDecisionView,
    TaskReadyView,
    TaskReviewView,
)
from mgbs_bot.utils import (
    ALLOWED_MENTIONS,
    add_banner,
    footer_text,
    logger,
    make_banner_file,
    store,
    task_leader_role_ids,
    task_welcome_embed,
)


def _overwrites_equal(
    current: dict,
    target: dict[disnake.Role | disnake.Member, disnake.PermissionOverwrite],
) -> bool:
    if len(current) != len(target):
        return False
    current_by_id = {getattr(key, "id", 0): value for key, value in current.items()}
    for key, overwrite in target.items():
        existing = current_by_id.get(getattr(key, "id", 0))
        if existing is None or existing.pair() != overwrite.pair():
            return False
    return True


class TaskBot(commands.InteractionBot):
    def __init__(self, **options):
        super().__init__(**options)
        store.initialize()
        self._decision_view_registered = False
        self._application_views_migrated = False
        self._task_room_views_registered = False
        self._task_rooms_reconciled = False
        self._task_room_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._permission_diagnostics_logged: set[tuple[str, int]] = set()
        if not BANNER_PATH.is_file():
            logger.warning("banner.png is missing; messages will be sent without the banner")
        self.add_cog(TaskCog(self))
        self.add_cog(PanelCog(self))
        self.add_cog(AfkReminderCog(self))

    def log_permission_diagnostics(
        self,
        guild: disnake.Guild,
        *,
        action: str,
        channel: disnake.abc.GuildChannel | None = None,
        required: tuple[str, ...] = (),
    ) -> list[str]:
        member = guild.me
        if member is None and self.user is not None:
            member = guild.get_member(self.user.id)
        if member is None:
            logger.error(
                "Permission check failed: bot member is not cached: action=%s guild_id=%s",
                action,
                guild.id,
            )
            return ["bot_member_not_cached"]

        permissions = member.guild_permissions if channel is None else channel.permissions_for(member)
        enabled = {
            name: bool(getattr(permissions, name, False))
            for name in (
                "manage_channels",
                "manage_roles",
                "view_channel",
                "send_messages",
                "read_message_history",
                "embed_links",
                "attach_files",
                "manage_messages",
                "connect",
                "speak",
            )
        }
        missing = [name for name in required if not enabled.get(name, False)]
        logger.info(
            "Permission diagnostics: action=%s guild_id=%s channel_id=%s missing=%s",
            action,
            guild.id,
            getattr(channel, "id", None),
            missing,
        )
        if missing:
            logger.error(
                "Bot is missing permissions for action=%s guild_id=%s: %s",
                action,
                guild.id,
                ", ".join(missing),
            )
        return missing

    def task_room_overwrites(
        self,
        guild: disnake.Guild,
        *,
        kind: str,
        creator: disnake.Member | None = None,
        participants: list[disnake.Member] | None = None,
        leader_role_ids: set[int] | None = None,
    ) -> dict[disnake.Role | disnake.Member, disnake.PermissionOverwrite]:
        overwrites: dict[disnake.Role | disnake.Member, disnake.PermissionOverwrite] = {
            guild.default_role: disnake.PermissionOverwrite(view_channel=False)
        }
        bot_member = guild.me
        if bot_member is None and self.user is not None:
            bot_member = guild.get_member(self.user.id)
        if bot_member is None:
            raise RuntimeError(
                f"Bot member is not cached in guild {guild.id}; cannot set private room permissions"
            )
        overwrites[bot_member] = disnake.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=True,
            embed_links=True,
            attach_files=True,
            manage_channels=True,
            manage_messages=True,
            connect=True,
            speak=True,
        )

        if creator is not None:
            creator_permissions = disnake.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
            )
            if kind == "info":
                creator_permissions.send_messages = False
            elif kind == "general":
                creator_permissions.send_messages = True
                creator_permissions.attach_files = True
                creator_permissions.embed_links = True
            elif kind == "voice":
                creator_permissions.connect = True
                creator_permissions.speak = True
            overwrites[creator] = creator_permissions

        if kind == "info":
            for member in participants or []:
                overwrites[member] = disnake.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=False,
                )
        elif kind == "general":
            for member in participants or []:
                overwrites[member] = disnake.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                    attach_files=True,
                    embed_links=True,
                )
            for role_id in leader_role_ids or set():
                role = guild.get_role(role_id)
                if role is not None:
                    overwrites[role] = disnake.PermissionOverwrite(
                        view_channel=True,
                        read_message_history=True,
                        send_messages=False,
                    )
        elif kind == "voice":
            for member in participants or []:
                overwrites[member] = disnake.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    speak=True,
                )
        return overwrites

    async def fetch_task_creator(
        self, guild: disnake.Guild, task: dict
    ) -> disnake.Member | None:
        creator = guild.get_member(task["created_by"])
        if creator is not None:
            return creator
        try:
            return await guild.fetch_member(task["created_by"])
        except disnake.NotFound:
            return None

    async def secure_task_room(self, guild: disnake.Guild, task: dict, room: dict) -> bool:
        approved = [
            item
            for item in store.list_applications(guild.id, task["id"])
            if item["status"] == "approved"
        ]
        members: list[disnake.Member] = []
        for application in approved:
            member = guild.get_member(application["user_id"])
            if member is None:
                try:
                    member = await guild.fetch_member(application["user_id"])
                except disnake.HTTPException:
                    continue
            members.append(member)

        category = guild.get_channel(room["category_id"])
        info = guild.get_channel(room["info_channel_id"])
        general = guild.get_channel(room["general_channel_id"])
        voice = guild.get_channel(room["voice_channel_id"])
        try:
            creator = await self.fetch_task_creator(guild, task)
            if not isinstance(category, disnake.CategoryChannel):
                category = await guild.fetch_channel(room["category_id"])
            if not isinstance(info, disnake.TextChannel):
                info = await guild.fetch_channel(room["info_channel_id"])
            if not isinstance(general, disnake.TextChannel):
                general = await guild.fetch_channel(room["general_channel_id"])
            if not isinstance(voice, disnake.VoiceChannel):
                voice = await guild.fetch_channel(room["voice_channel_id"])

            edit_specs = (
                (
                    category,
                    self.task_room_overwrites(guild, kind="category", creator=creator),
                    f"Secure task #{task['id']} category",
                ),
                (
                    info,
                    self.task_room_overwrites(
                        guild, kind="info", creator=creator, participants=members
                    ),
                    f"Secure task #{task['id']} information channel",
                ),
                (
                    general,
                    self.task_room_overwrites(
                        guild,
                        kind="general",
                        creator=creator,
                        participants=members,
                        leader_role_ids=(
                            task_leader_role_ids(task["role_ids"])
                            if room["status"] == "ready_for_review"
                            else set()
                        ),
                    ),
                    f"Secure task #{task['id']} team channel",
                ),
                (
                    voice,
                    self.task_room_overwrites(
                        guild, kind="voice", creator=creator, participants=members
                    ),
                    f"Secure task #{task['id']} voice channel",
                ),
            )
            for channel, target_overwrites, reason in edit_specs:
                if _overwrites_equal(channel.overwrites, target_overwrites):
                    continue
                await channel.edit(overwrites=target_overwrites, reason=reason)
            return True
        except disnake.NotFound:
            logger.warning(
                "Task #%s room record points to a channel that was deleted", task["id"]
            )
            return False
        except (disnake.HTTPException, AttributeError):
            logger.exception(
                "Could not secure task room permissions for task #%s", task["id"]
            )
            return False

    async def task_room_has_missing_channel(self, guild: disnake.Guild, room: dict) -> bool:
        for channel_id in (
            room["category_id"],
            room["info_channel_id"],
            room["general_channel_id"],
            room["voice_channel_id"],
        ):
            try:
                await guild.fetch_channel(channel_id)
            except disnake.NotFound:
                return True
            except disnake.HTTPException:
                logger.exception("Could not check saved task channel %s", channel_id)
                return False
        return False

    async def repair_task_rooms(
        self, guild: disnake.Guild, task: dict, room: dict
    ) -> None:
        creator = await self.fetch_task_creator(guild, task)
        category = await self._fetch_room_resource(
            guild, room["category_id"], disnake.CategoryChannel
        )
        category_recreated = category is None
        if category is None:
            category_name = (
                " ".join(str(task["title"]).split())[:100] or f"Задание #{task['id']}"
            )
            category = await guild.create_category(
                name=category_name,
                overwrites=self.task_room_overwrites(guild, kind="category", creator=creator),
                reason=f"Repair task #{task['id']} work area",
            )

        info = await self._fetch_room_resource(
            guild, room["info_channel_id"], disnake.TextChannel
        )
        info_recreated = info is None
        if info is None:
            info = await guild.create_text_channel(
                name="info",
                category=category,
                overwrites=self.task_room_overwrites(guild, kind="info", creator=creator),
                reason=f"Repair task #{task['id']} information",
            )

        general = await self._fetch_room_resource(
            guild, room["general_channel_id"], disnake.TextChannel
        )
        general_recreated = general is None
        if general is None:
            general = await guild.create_text_channel(
                name="general",
                category=category,
                overwrites=self.task_room_overwrites(guild, kind="general", creator=creator),
                topic=f"Общий чат команды для задания #{task['id']}",
                reason=f"Repair task #{task['id']} team chat",
            )

        voice = await self._fetch_room_resource(
            guild, room["voice_channel_id"], disnake.VoiceChannel
        )
        voice_recreated = voice is None
        if voice is None:
            voice = await guild.create_voice_channel(
                name="voice",
                category=category,
                overwrites=self.task_room_overwrites(guild, kind="voice", creator=creator),
                reason=f"Repair task #{task['id']} voice chat",
            )

        if category_recreated:
            for channel, was_recreated in (
                (info, info_recreated),
                (general, general_recreated),
                (voice, voice_recreated),
            ):
                if not was_recreated:
                    await channel.edit(
                        category=category, reason=f"Repair task #{task['id']} category"
                    )

        info_message_id = None if info_recreated else room.get("info_message_id")
        if info_message_id is not None:
            try:
                await info.fetch_message(info_message_id)
            except disnake.NotFound:
                info_message_id = None
            except disnake.HTTPException:
                logger.exception(
                    "Could not verify the welcome message for task #%s", task["id"]
                )
                info_message_id = None

        store.save_task_rooms(
            task_id=task["id"],
            guild_id=guild.id,
            category_id=category.id,
            info_channel_id=info.id,
            general_channel_id=general.id,
            voice_channel_id=voice.id,
            info_message_id=info_message_id,
        )
        logger.info(
            "Repaired task #%s channels: category=%s info=%s general=%s voice=%s",
            task["id"],
            category.id,
            info.id,
            general.id,
            voice.id,
        )

        username = creator.display_name if creator is not None else "Создатель"
        if info_message_id is None:
            try:
                options: dict = {
                    "embed": task_welcome_embed(task, 0, username),
                    "view": TaskReadyView(self, task["id"]),
                    "allowed_mentions": disnake.AllowedMentions(roles=True),
                }
                banner = make_banner_file()
                if banner is not None:
                    options["file"] = banner
                welcome = await info.send(**options)
                store.set_task_info_message(task["id"], welcome.id)
            except Exception:
                logger.exception(
                    "Could not restore the welcome message for task #%s", task["id"]
                )

        if general_recreated:
            try:
                instruction_embed = disnake.Embed(
                    title="Общий чат команды",
                    description=(
                        "Обсуждайте здесь ход работы.\n\n"
                        "Когда работа будет готова, закрепите итоговую версию в этом "
                        "канале для проверки."
                    ),
                    color=EMBED_COLOR,
                )
                instruction_embed.set_footer(text=footer_text(username))
                instruction_banner = add_banner(instruction_embed)
                options = {"embed": instruction_embed, "allowed_mentions": ALLOWED_MENTIONS}
                if instruction_banner is not None:
                    options["file"] = instruction_banner
                instruction = await general.send(**options)
                try:
                    await instruction.pin(reason="Инструкция по проверке готовой работы")
                except disnake.HTTPException:
                    logger.warning(
                        "Could not pin repaired task instructions in channel %s", general.id
                    )
            except Exception:
                logger.exception(
                    "Could not restore task instructions for task #%s", task["id"]
                )

    async def _fetch_room_resource(self, guild: disnake.Guild, channel_id: int, expected_type):
        try:
            channel = await guild.fetch_channel(channel_id)
        except disnake.NotFound:
            return None
        return channel if isinstance(channel, expected_type) else None

    async def create_task_rooms(
        self, guild: disnake.Guild, task: dict | None
    ) -> list[str]:
        if task is None:
            raise RuntimeError("The task disappeared before its channels were created")
        lock = self._task_room_locks.setdefault((guild.id, task["id"]), asyncio.Lock())
        async with lock:
            return await self._create_task_rooms_locked(guild, task)

    async def _create_task_rooms_locked(
        self, guild: disnake.Guild, task: dict
    ) -> list[str]:
        missing = self.log_permission_diagnostics(
            guild,
            action=f"create rooms for task #{task['id']}",
            required=("manage_channels",),
        )
        if missing:
            raise PermissionError(
                "Bot needs the Manage Channels server permission to create task rooms; "
                f"missing effective permissions: {', '.join(missing)}"
            )

        creator = await self.fetch_task_creator(guild, task)
        progress = store.get_task_room_progress(task["id"])
        if progress is None:
            progress = {
                "category_id": None,
                "info_channel_id": None,
                "general_channel_id": None,
                "voice_channel_id": None,
            }
            store.save_task_room_progress(task_id=task["id"], guild_id=guild.id, **progress)

        category = None
        if progress["category_id"] is not None:
            category = await self._fetch_room_resource(
                guild, progress["category_id"], disnake.CategoryChannel
            )
        info = None
        if progress["info_channel_id"] is not None:
            info = await self._fetch_room_resource(
                guild, progress["info_channel_id"], disnake.TextChannel
            )
        general = None
        if progress["general_channel_id"] is not None:
            general = await self._fetch_room_resource(
                guild, progress["general_channel_id"], disnake.TextChannel
            )
        voice = None
        if progress["voice_channel_id"] is not None:
            voice = await self._fetch_room_resource(
                guild, progress["voice_channel_id"], disnake.VoiceChannel
            )

        category_id = category.id if category is not None else None
        info_channel_id = info.id if info is not None else None
        general_channel_id = general.id if general is not None else None
        voice_channel_id = voice.id if voice is not None else None

        def save_progress() -> None:
            store.save_task_room_progress(
                task_id=task["id"],
                guild_id=guild.id,
                category_id=category_id,
                info_channel_id=info_channel_id,
                general_channel_id=general_channel_id,
                voice_channel_id=voice_channel_id,
            )

        save_progress()
        stage = "create_category"
        message_permissions = ["view_channel", "send_messages", "read_message_history", "embed_links"]
        if BANNER_PATH.is_file():
            message_permissions.append("attach_files")
        try:
            category_name = (
                " ".join(str(task["title"]).split())[:100] or f"Задание #{task['id']}"
            )
            if category is None:
                category = await guild.create_category(
                    name=category_name,
                    overwrites=self.task_room_overwrites(
                        guild, kind="category", creator=creator
                    ),
                    reason=f"Task #{task['id']} work area",
                )
                category_id = category.id
                save_progress()
                logger.info(
                    "Task room progress: stage=category_created guild_id=%s task_id=%s category_id=%s",
                    guild.id,
                    task["id"],
                    category.id,
                )
            self.log_permission_diagnostics(
                guild,
                action=f"use category for task #{task['id']}",
                channel=category,
                required=("view_channel", "manage_channels"),
            )

            stage = "create_info_channel"
            if info is None:
                info = await guild.create_text_channel(
                    name="info",
                    category=category,
                    overwrites=self.task_room_overwrites(
                        guild, kind="info", creator=creator
                    ),
                    reason=f"Task #{task['id']} information",
                )
                info_channel_id = info.id
                save_progress()
                logger.info(
                    "Task room progress: stage=info_created guild_id=%s task_id=%s channel_id=%s",
                    guild.id,
                    task["id"],
                    info.id,
                )
            elif info.category_id != category.id:
                stage = "move_info_channel"
                await info.edit(
                    category=category,
                    overwrites=self.task_room_overwrites(
                        guild, kind="info", creator=creator
                    ),
                    reason=f"Resume task #{task['id']} information channel",
                )
            self.log_permission_diagnostics(
                guild,
                action=f"send task welcome for #{task['id']}",
                channel=info,
                required=tuple(message_permissions),
            )

            stage = "create_general_channel"
            if general is None:
                general = await guild.create_text_channel(
                    name="general",
                    category=category,
                    overwrites=self.task_room_overwrites(
                        guild, kind="general", creator=creator
                    ),
                    topic=f"Общий чат команды для задания #{task['id']}",
                    reason=f"Task #{task['id']} team chat",
                )
                general_channel_id = general.id
                save_progress()
                logger.info(
                    "Task room progress: stage=general_created guild_id=%s task_id=%s channel_id=%s",
                    guild.id,
                    task["id"],
                    general.id,
                )
            elif general.category_id != category.id:
                stage = "move_general_channel"
                await general.edit(
                    category=category,
                    overwrites=self.task_room_overwrites(
                        guild, kind="general", creator=creator
                    ),
                    reason=f"Resume task #{task['id']} team channel",
                )
            self.log_permission_diagnostics(
                guild,
                action=f"send task instructions for #{task['id']}",
                channel=general,
                required=tuple(message_permissions + ["manage_messages"]),
            )

            stage = "create_voice_channel"
            if voice is None:
                voice = await guild.create_voice_channel(
                    name="voice",
                    category=category,
                    overwrites=self.task_room_overwrites(
                        guild, kind="voice", creator=creator
                    ),
                    reason=f"Task #{task['id']} voice chat",
                )
                voice_channel_id = voice.id
                save_progress()
                logger.info(
                    "Task room progress: stage=voice_created guild_id=%s task_id=%s channel_id=%s",
                    guild.id,
                    task["id"],
                    voice.id,
                )
            elif voice.category_id != category.id:
                stage = "move_voice_channel"
                await voice.edit(
                    category=category,
                    overwrites=self.task_room_overwrites(
                        guild, kind="voice", creator=creator
                    ),
                    reason=f"Resume task #{task['id']} voice channel",
                )
            self.log_permission_diagnostics(
                guild,
                action=f"use voice channel for task #{task['id']}",
                channel=voice,
                required=("view_channel", "connect", "speak"),
            )

            stage = "save_room_record"
            store.save_task_rooms(
                task_id=task["id"],
                guild_id=guild.id,
                category_id=category.id,
                info_channel_id=info.id,
                general_channel_id=general.id,
                voice_channel_id=voice.id,
            )
            logger.info(
                "Task room setup completed: guild_id=%s task_id=%s category_id=%s info_id=%s general_id=%s voice_id=%s",
                guild.id,
                task["id"],
                category.id,
                info.id,
                general.id,
                voice.id,
            )
        except Exception as exc:
            logger.exception(
                "Task room setup failed: stage=%s guild_id=%s task_id=%s exception=%s; created channels are preserved",
                stage,
                guild.id,
                task["id"],
                type(exc).__name__,
            )
            raise

        warnings: list[str] = []
        username = creator.display_name if creator is not None else "Создатель"
        try:
            options = {
                "embed": task_welcome_embed(task, 0, username),
                "view": TaskReadyView(self, task["id"]),
                "allowed_mentions": disnake.AllowedMentions(roles=True),
            }
            banner = make_banner_file()
            if banner is not None:
                options["file"] = banner
            welcome_message = await info.send(**options)
            store.set_task_info_message(task["id"], welcome_message.id)
            logger.info(
                "Task welcome sent: guild_id=%s task_id=%s message_id=%s",
                guild.id,
                task["id"],
                welcome_message.id,
            )
        except Exception:
            logger.exception(
                "Task rooms exist but welcome send/save failed: guild_id=%s task_id=%s channel_id=%s",
                guild.id,
                task["id"],
                info.id,
            )
            warnings.append("приветствие и кнопка отметки готовности")

        try:
            instruction_embed = disnake.Embed(
                title="Общий чат команды",
                description=(
                    "Обсуждайте здесь ход работы.\n\n"
                    "Когда работа будет готова, закрепите итоговую версию в этом канале "
                    "для проверки."
                ),
                color=EMBED_COLOR,
            )
            instruction_embed.set_footer(text=footer_text(username))
            instruction_banner = add_banner(instruction_embed)
            options = {"embed": instruction_embed, "allowed_mentions": ALLOWED_MENTIONS}
            if instruction_banner is not None:
                options["file"] = instruction_banner
            instruction = await general.send(**options)
            try:
                await instruction.pin(reason="Инструкция по проверке готовой работы")
                logger.info(
                    "Task instructions sent and pinned: task_id=%s channel_id=%s",
                    task["id"],
                    general.id,
                )
            except disnake.HTTPException:
                logger.exception(
                    "Task instructions sent but could not be pinned: task_id=%s channel_id=%s",
                    task["id"],
                    general.id,
                )
        except Exception:
            logger.exception(
                "Task rooms exist but general-instructions send failed: task_id=%s channel_id=%s",
                task["id"],
                general.id,
            )
            warnings.append("инструкция в общем канале")
        return warnings

    async def add_task_participant(
        self,
        guild: disnake.Guild,
        task: dict,
        user_id: int,
        *,
        update_info: bool = True,
    ) -> bool:
        room = store.get_task_room(guild.id, task["id"])
        if room is None or room["status"] != "active":
            return False
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except disnake.HTTPException:
                logger.exception("Could not fetch task participant %s", user_id)
                return False

        general = guild.get_channel(room["general_channel_id"])
        voice = guild.get_channel(room["voice_channel_id"])
        info = guild.get_channel(room["info_channel_id"])
        try:
            if not isinstance(general, disnake.TextChannel):
                general = await guild.fetch_channel(room["general_channel_id"])
            if not isinstance(voice, disnake.VoiceChannel):
                voice = await guild.fetch_channel(room["voice_channel_id"])
            await general.set_permissions(
                member,
                view_channel=True,
                read_message_history=True,
                send_messages=True,
                attach_files=True,
                embed_links=True,
            )
            if not isinstance(info, disnake.TextChannel):
                info = await guild.fetch_channel(room["info_channel_id"])
            await info.set_permissions(
                member,
                view_channel=True,
                read_message_history=True,
                send_messages=False,
            )
            await voice.set_permissions(member, view_channel=True, connect=True, speak=True)
        except disnake.HTTPException:
            logger.exception("Could not grant task channel access to participant %s", user_id)
            return False

        if update_info and isinstance(info, disnake.TextChannel) and room["info_message_id"]:
            applications = store.list_applications(guild.id, task["id"])
            participant_count = sum(item["status"] == "approved" for item in applications)
            try:
                message = await info.fetch_message(room["info_message_id"])
                creator = guild.get_member(task["created_by"])
                username = creator.display_name if creator is not None else "Создатель"
                embed = task_welcome_embed(task, participant_count, username)
                await message.edit(embed=embed, attachments=message.attachments)
            except disnake.HTTPException:
                logger.exception(
                    "Could not update participant count for task #%s", task["id"]
                )
        return True

    async def delete_task_room_resources(self, room: dict | None) -> int:
        if room is None:
            return 0
        guild = self.get_guild(room["guild_id"])
        if guild is None:
            return 4
        failures = 0
        for channel_id in (
            room["info_channel_id"],
            room["general_channel_id"],
            room["voice_channel_id"],
            room["category_id"],
        ):
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except disnake.NotFound:
                    continue
                except disnake.HTTPException:
                    failures += 1
                    logger.exception("Could not fetch task channel %s for deletion", channel_id)
                    continue
            try:
                await channel.delete(reason="Task completed or deleted")
            except disnake.NotFound:
                continue
            except disnake.HTTPException:
                failures += 1
                logger.exception("Could not delete task channel %s", channel_id)
        return failures

    async def grant_task_leader_review_access(
        self, guild: disnake.Guild, task: dict
    ) -> bool:
        room = store.get_task_room(guild.id, task["id"])
        if room is None:
            return False
        general = guild.get_channel(room["general_channel_id"])
        if not isinstance(general, disnake.TextChannel):
            try:
                general = await guild.fetch_channel(room["general_channel_id"])
            except disnake.HTTPException:
                logger.exception(
                    "Could not fetch the general channel for task #%s", task["id"]
                )
                return False
        try:
            for role_id in task_leader_role_ids(task["role_ids"]):
                role = guild.get_role(role_id)
                if role is not None:
                    await general.set_permissions(
                        role,
                        view_channel=True,
                        read_message_history=True,
                        send_messages=False,
                    )
            return True
        except disnake.HTTPException:
            logger.exception(
                "Could not grant task review access for task #%s", task["id"]
            )
            return False

    async def apply_task_promotions(
        self,
        guild: disnake.Guild,
        participants: list[tuple[int, str | None]],
    ) -> list[str]:
        hierarchies = {
            "builder": BUILDER_ROLES,
            "modeller": MODELLER_ROLES,
            "scripter": SCRIPTER_ROLES,
        }
        results: list[str] = []
        for user_id, track in participants:
            if track is None or ":" not in track:
                continue
            track_name, current_index_text = track.split(":", maxsplit=1)
            hierarchy = hierarchies.get(track_name)
            if hierarchy is None or not current_index_text.isdecimal():
                continue
            current_index = int(current_index_text)
            if current_index <= 0 or current_index >= len(hierarchy):
                continue
            progress = store.get_task_progress(guild.id, user_id, track)
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except disnake.HTTPException:
                    logger.exception(
                        "Could not fetch member %s for task promotion", user_id
                    )
                    continue

            held = {role.id for role in member.roles}
            if hierarchy[current_index] not in held:
                continue
            if current_index == len(hierarchy) - 1:
                eligible = progress["completed"] >= 5 and progress["rated_7"] >= 1
            elif current_index == len(hierarchy) - 2:
                eligible = progress["completed"] >= 10 and progress["rated_9"] >= 3
            else:
                eligible = False
            if not eligible:
                continue

            old_role = guild.get_role(hierarchy[current_index])
            new_role = guild.get_role(hierarchy[current_index - 1])
            if old_role is None or new_role is None:
                logger.error("Task promotion roles are missing for member %s", user_id)
                continue
            try:
                await member.add_roles(
                    new_role, reason="Автоматическое повышение за выполненные задания"
                )
                await member.remove_roles(
                    old_role, reason="Автоматическое повышение за выполненные задания"
                )
                results.append(
                    f"Участник {member.mention} повышен до **{new_role.name}**."
                )
            except disnake.HTTPException:
                logger.exception(
                    "Could not promote member %s after task completion", user_id
                )
                results.append(
                    f"Не удалось выдать повышение участнику {member.mention}; "
                    "проверьте права бота."
                )
        return results

    async def on_ready(self) -> None:
        for guild in self.guilds:
            marker = ("guild", guild.id)
            if marker in self._permission_diagnostics_logged:
                continue
            self.log_permission_diagnostics(
                guild,
                action="startup bot-permission preflight",
                required=("manage_channels", "manage_roles", "manage_messages"),
            )
            self._permission_diagnostics_logged.add(marker)
            bot_member = guild.me
            if bot_member is None:
                continue
            unmanageable_roles = [
                f"{role_id}:{ROLE_NAMES[role_id]}"
                for role_id in ROLE_NAMES
                if (role := guild.get_role(role_id)) is not None
                and role.position >= bot_member.top_role.position
            ]
            if unmanageable_roles:
                logger.error(
                    "Bot role is not high enough to manage configured task roles: "
                    "guild_id=%s bot_top_role_id=%s roles=%s",
                    guild.id,
                    bot_member.top_role.id,
                    unmanageable_roles,
                )
            else:
                logger.info(
                    "Bot role hierarchy check passed: guild_id=%s bot_top_role_id=%s",
                    guild.id,
                    bot_member.top_role.id,
                )

        for channel_id, action, required in (
            (
                APPLICATIONS_CHANNEL_ID,
                "applications channel",
                (
                    "view_channel",
                    "read_message_history",
                    "send_messages",
                    "embed_links",
                    "attach_files",
                    "manage_messages",
                ),
            ),
            (
                TASK_LOG_CHANNEL_ID,
                "task-log channel",
                ("view_channel", "send_messages", "embed_links", "attach_files"),
            ),
        ):
            marker = ("channel", channel_id)
            if marker in self._permission_diagnostics_logged:
                continue
            channel = self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except disnake.HTTPException:
                    logger.exception(
                        "Could not fetch configured %s: channel_id=%s", action, channel_id
                    )
                    self._permission_diagnostics_logged.add(marker)
                    continue
            if not isinstance(channel, disnake.TextChannel):
                logger.error(
                    "Configured %s is not a text channel: channel_id=%s", action, channel_id
                )
            else:
                self.log_permission_diagnostics(
                    channel.guild,
                    action=f"startup {action} preflight",
                    channel=channel,
                    required=required,
                )
            self._permission_diagnostics_logged.add(marker)

        if not self._decision_view_registered:
            self.add_view(ApplicationDecisionView(self))
            self._decision_view_registered = True
        if not self._application_views_migrated:
            self._application_views_migrated = await self.refresh_pending_application_views()
        if not self._task_room_views_registered:
            self._task_room_views_registered = self.register_task_room_views()
        if not self._task_rooms_reconciled:
            self._task_rooms_reconciled = await self.reconcile_open_task_rooms()
        logger.info("Бот запущен как %s", self.user)

    def register_task_room_views(self) -> bool:
        for room in store.list_active_task_rooms():
            message_id = room.get("info_message_id")
            if message_id is None:
                continue
            if room["room_status"] == "ready_for_review":
                view = TaskReviewView(self, room["id"])
            else:
                view = TaskReadyView(self, room["id"])
            self.add_view(view, message_id=message_id)
        return True

    async def reconcile_open_task_rooms(self) -> bool:
        successful = True
        for guild in self.guilds:
            for task in store.list_tasks(guild.id):
                room = store.get_task_room(guild.id, task["id"])
                if room is None:
                    try:
                        await self.create_task_rooms(guild, task)
                    except Exception:
                        logger.exception(
                            "Could not create missing task channels for task #%s",
                            task["id"],
                        )
                        successful = False
                        continue
                    room = store.get_task_room(guild.id, task["id"])

                if room is None or room["status"] not in {"active", "ready_for_review"}:
                    continue
                if not await self.secure_task_room(guild, task, room):
                    if room["status"] != "active" or not await self.task_room_has_missing_channel(
                        guild, room
                    ):
                        successful = False
                        continue
                    try:
                        await self.repair_task_rooms(guild, task, room)
                    except Exception:
                        logger.exception(
                            "Could not repair missing channels for task #%s", task["id"]
                        )
                        successful = False
                        continue
                    room = store.get_task_room(guild.id, task["id"])
                    if room is None or not await self.secure_task_room(guild, task, room):
                        successful = False
                        continue
                if room["status"] == "ready_for_review":
                    continue
                approved = [
                    item
                    for item in store.list_applications(guild.id, task["id"])
                    if item["status"] == "approved"
                ]
                for index, application in enumerate(approved):
                    if not await self.add_task_participant(
                        guild,
                        task,
                        application["user_id"],
                        update_info=index == len(approved) - 1,
                    ):
                        successful = False
        return successful

    async def refresh_pending_application_views(self) -> bool:
        pending = store.list_pending_applications()
        if not pending:
            return True

        channel = self.get_channel(APPLICATIONS_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(APPLICATIONS_CHANNEL_ID)
            except disnake.HTTPException:
                logger.exception("Не удалось открыть канал заявок для обновления селекторов")
                return False
        if not isinstance(channel, disnake.TextChannel):
            logger.error("Канал заявок имеет неподходящий тип; селекторы не обновлены")
            return False

        for application in pending:
            message_id = application.get("message_id")
            if message_id is None:
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(view=ApplicationDecisionView(self))
            except disnake.NotFound:
                continue
            except disnake.HTTPException:
                logger.exception("Не удалось обновить селектор заявки %s", message_id)
        return True
