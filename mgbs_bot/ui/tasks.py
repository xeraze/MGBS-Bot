from __future__ import annotations

import disnake
from disnake.ext import commands

from mgbs_bot.config import APPLICATIONS_CHANNEL_ID, EMBED_COLOR, ROLE_NAMES, TASK_ROLE_IDS
from mgbs_bot.utils import (
    ALLOWED_MENTIONS,
    add_banner,
    application_approver_role_ids,
    code_block,
    footer_text,
    local_now,
    logger,
    member_is_task_creator,
    send_task_log,
    store,
    task_leader_role_ids,
    task_progression_track,
    task_roles_text,
    task_welcome_embed,
)


class TaskCreateModal(disnake.ui.Modal):
    def __init__(self, bot: commands.InteractionBot, creator_id: int):
        self.bot = bot
        self.creator_id = creator_id
        role_select = disnake.ui.StringSelect(
            custom_id="task_create:roles",
            placeholder="Выберите одну или несколько ролей",
            min_values=1,
            max_values=len(TASK_ROLE_IDS),
            options=[
                disnake.SelectOption(label=ROLE_NAMES[role_id], value=str(role_id))
                for role_id in TASK_ROLE_IDS
            ],
        )
        title_input = disnake.ui.TextInput(
            custom_id="task_create:title",
            style=disnake.TextInputStyle.short,
            placeholder="Короткое название задания",
            min_length=2,
            max_length=80,
        )
        description_input = disnake.ui.TextInput(
            custom_id="task_create:description",
            style=disnake.TextInputStyle.paragraph,
            placeholder="Опишите, что нужно сделать",
            min_length=3,
            max_length=1200,
        )
        deadline_input = disnake.ui.TextInput(
            custom_id="task_create:deadline",
            style=disnake.TextInputStyle.short,
            placeholder="Например: 25.10.2026 20:00",
            min_length=2,
            max_length=80,
        )
        difficulty_select = disnake.ui.StringSelect(
            custom_id="task_create:difficulty",
            placeholder="Выберите сложность",
            min_values=1,
            max_values=1,
            options=[
                disnake.SelectOption(label=label, value=label)
                for label in ("Лёгкая", "Средняя", "Тяжёлая", "Профессиональная")
            ],
        )
        super().__init__(
            title="Создать задание",
            custom_id=f"task_create:{creator_id}",
            components=[
                disnake.ui.Label("Название задания", title_input),
                disnake.ui.Label("Для каких ролей", role_select),
                disnake.ui.Label("Что нужно сделать", description_input),
                disnake.ui.Label("Дедлайн", deadline_input),
                disnake.ui.Label("Сложность", difficulty_select),
            ],
            timeout=600,
        )

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        if inter.author.id != self.creator_id or inter.guild is None:
            await inter.response.send_message(
                "Это меню создано для другого пользователя.", ephemeral=True
            )
            return

        raw_role_ids = inter.values.get("task_create:roles", [])
        raw_difficulty = inter.values.get("task_create:difficulty", [])
        try:
            role_ids = [int(value) for value in raw_role_ids]
        except (TypeError, ValueError):
            await inter.response.send_message(
                "Не удалось прочитать выбранные роли.", ephemeral=True
            )
            return
        role_ids = [role_id for role_id in role_ids if role_id in ROLE_NAMES]
        title = inter.text_values.get("task_create:title", "").strip()
        description = inter.text_values.get("task_create:description", "").strip()
        deadline = inter.text_values.get("task_create:deadline", "").strip()
        difficulty = raw_difficulty[0] if raw_difficulty else ""
        if not role_ids or not title or not description or not deadline or not difficulty:
            await inter.response.send_message("Заполните все поля задания.", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)
        task_id = store.create_task(
            guild_id=inter.guild.id,
            title=title,
            description=description,
            deadline=deadline,
            difficulty=difficulty,
            role_ids=role_ids,
            created_by=inter.author.id,
            created_at=local_now().isoformat(),
        )
        try:
            room_warnings = await self.bot.create_task_rooms(
                inter.guild, store.get_task(inter.guild.id, task_id)
            )
        except Exception as exc:
            logger.exception(
                "Could not finish task channels: task_id=%s guild_id=%s exception=%s",
                task_id,
                inter.guild.id,
                type(exc).__name__,
            )
            if isinstance(exc, PermissionError) and "Manage Channels" in str(exc):
                reason = (
                    "У бота не хватает серверного права **Управлять каналами**. "
                    "Назначьте его роли бота в настройках сервера."
                )
            elif isinstance(exc, PermissionError):
                reason = (
                    "Не удалось проверить участника бота на сервере. Проверьте, что бот не был "
                    "удалён и имеет доступ к серверу. Диагностика записана в `data/logs/bot.log`."
                )
            else:
                reason = (
                    "Уже созданные каналы сохранены; бот продолжит настройку при следующем "
                    "запуске. Подробности записаны в `data/logs/bot.log`."
                )
            await inter.edit_original_response(
                content=(
                    f"Задание **#{task_id}** сохранено, но настройку каналов завершить не "
                    f"удалось. {reason}"
                )
            )
            return

        await send_task_log(
            self.bot,
            inter.guild,
            title="Создано задание",
            description=f"**{title}** · ``#{task_id}``",
            actor=inter.author.display_name,
            fields=[
                ("Роли", task_roles_text(role_ids)),
                ("Дедлайн", deadline),
                ("Сложность", difficulty),
                ("Описание", description),
            ],
        )

        embed = disnake.Embed(
            title="Задание создано",
            description=f"**{title}** · ``#{task_id}`` · ``{inter.author.display_name}``",
            color=EMBED_COLOR,
        )
        if room_warnings:
            embed.add_field(
                name="⚠️ Каналы созданы не полностью",
                value="Не удалось отправить: "
                + ", ".join(room_warnings)
                + ". Причина есть в логе бота.",
                inline=False,
            )
        task_room = store.get_task_room(inter.guild.id, task_id)
        if task_room is not None:
            embed.add_field(
                name="📁 Каналы задания", value=f"<#{task_room['info_channel_id']}>", inline=False
            )
        embed.add_field(name="🆔 Номер задания", value=code_block(f"#{task_id}", 80), inline=False)
        embed.add_field(name="🎭 Роли", value=code_block(task_roles_text(role_ids), 950), inline=False)
        embed.add_field(name="⏳ Дедлайн", value=code_block(deadline, 200), inline=False)
        embed.add_field(name="⚙️ Сложность", value=code_block(difficulty, 100), inline=False)
        embed.add_field(
            name="📝 Что нужно сделать", value=code_block(description, 950), inline=False
        )
        embed.set_footer(
            text=footer_text(inter.author.display_name), icon_url=inter.author.display_avatar.url
        )
        banner = add_banner(embed)
        options: dict = {"embed": embed, "allowed_mentions": ALLOWED_MENTIONS}
        if banner is not None:
            options["file"] = banner
        await inter.edit_original_response(**options)


class TaskReadyView(disnake.ui.View):
    def __init__(self, bot: commands.InteractionBot, task_id: int):
        super().__init__(timeout=None)
        self.add_item(TaskReadySelect(bot, task_id))


class TaskReadySelect(disnake.ui.StringSelect):
    def __init__(self, bot: commands.InteractionBot, task_id: int):
        self.bot = bot
        self.task_id = task_id
        super().__init__(
            custom_id=f"task-room:ready:{task_id}",
            placeholder="Отметить готовность работы",
            min_values=1,
            max_values=1,
            options=[disnake.SelectOption(label="Работа готова", value="ready", emoji="✅")],
        )

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if inter.guild is None or not isinstance(inter.author, disnake.Member):
            await inter.response.send_message(
                "Действие доступно только на сервере.", ephemeral=True
            )
            return
        task = store.get_task(inter.guild.id, self.task_id)
        room = store.get_task_room(inter.guild.id, self.task_id)
        if task is None or room is None or room["status"] != "active":
            await inter.response.send_message(
                "Это задание уже отправлено на проверку или закрыто.", ephemeral=True
            )
            return
        participant = next(
            (
                item
                for item in store.list_applications(inter.guild.id, self.task_id)
                if item["user_id"] == inter.author.id and item["status"] == "approved"
            ),
            None,
        )
        if participant is None:
            await inter.response.send_message(
                "Отметить готовность могут только участники задания.", ephemeral=True
            )
            return
        if not store.set_task_room_status(
            inter.guild.id, self.task_id, "active", "ready_for_review"
        ):
            await inter.response.send_message(
                "Задание уже отправлено на проверку.", ephemeral=True
            )
            return

        applications = store.list_applications(inter.guild.id, self.task_id)
        count = sum(item["status"] == "approved" for item in applications)
        embed = task_welcome_embed(task, count, inter.author.display_name)
        embed.description += "\n\n✅ Работа отмечена как готовая. Ожидает оценки лидера."
        await inter.response.edit_message(
            embed=embed,
            attachments=inter.message.attachments,
            view=TaskReviewView(self.bot, self.task_id),
        )

        if not await self.bot.grant_task_leader_review_access(inter.guild, task):
            logger.warning("Could not grant leaders read access to task #%s", self.task_id)

        leaders = task_leader_role_ids(task["role_ids"])
        mention = " ".join(f"<@&{role_id}>" for role_id in sorted(leaders))
        try:
            await inter.channel.send(
                content=f"✅ Работа по заданию **#{self.task_id}** готова к проверке. {mention}".strip(),
                allowed_mentions=disnake.AllowedMentions(roles=True),
            )
        except disnake.HTTPException:
            logger.exception("Could not notify task leaders for task #%s", self.task_id)
        await send_task_log(
            self.bot,
            inter.guild,
            title="Работа отправлена на проверку",
            description=f"Задание ``#{self.task_id}`` — **{task['title']}**.",
            actor=inter.author.display_name,
            fields=[("Участник", str(inter.author)), ("Канал команды", f"#{inter.channel.name}")],
        )


class TaskReviewView(disnake.ui.View):
    def __init__(self, bot: commands.InteractionBot, task_id: int):
        super().__init__(timeout=None)
        self.add_item(TaskReviewSelect(bot, task_id))


class TaskReviewSelect(disnake.ui.StringSelect):
    def __init__(self, bot: commands.InteractionBot, task_id: int):
        self.bot = bot
        self.task_id = task_id
        super().__init__(
            custom_id=f"task-room:review:{task_id}",
            placeholder="Оценка работы лидером",
            min_values=1,
            max_values=1,
            options=[disnake.SelectOption(label="Оценить работу", value="review", emoji="⭐")],
        )

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if inter.guild is None or not isinstance(inter.author, disnake.Member):
            await inter.response.send_message("Оценка доступна только на сервере.", ephemeral=True)
            return
        task = store.get_task(inter.guild.id, self.task_id)
        room = store.get_task_room(inter.guild.id, self.task_id)
        if task is None or room is None or room["status"] != "ready_for_review":
            await inter.response.send_message(
                "Это задание сейчас не ожидает оценки.", ephemeral=True
            )
            return
        allowed_roles = task_leader_role_ids(task["role_ids"])
        if not {role.id for role in inter.author.roles}.intersection(allowed_roles):
            await inter.response.send_message(
                "Оценить работу может только лидер нужного направления.", ephemeral=True
            )
            return
        if inter.response.is_done():
            logger.warning("Task-review interaction already acknowledged: %s", inter.id)
            await inter.followup.send(
                "Запрос уже был обработан. Повторите оценку.", ephemeral=True
            )
            return
        modal = TaskReviewModal(self.bot, self.task_id, inter.author.id, inter.guild.id)
        try:
            await inter.response.send_modal(modal)
        except disnake.HTTPException:
            logger.exception("Could not open task-review modal: %s", inter.id)
            await inter.followup.send(
                "Не удалось открыть форму оценки. Повторите попытку.", ephemeral=True
            )


class TaskReviewModal(disnake.ui.Modal):
    def __init__(
        self,
        bot: commands.InteractionBot,
        task_id: int,
        reviewer_id: int,
        guild_id: int,
    ):
        self.bot = bot
        self.task_id = task_id
        self.reviewer_id = reviewer_id
        self.guild_id = guild_id
        speed = disnake.ui.TextInput(
            custom_id="task_review:speed",
            style=disnake.TextInputStyle.short,
            placeholder="Число от 1 до 10",
            min_length=1,
            max_length=2,
        )
        quality = disnake.ui.TextInput(
            custom_id="task_review:quality",
            style=disnake.TextInputStyle.short,
            placeholder="Число от 1 до 10",
            min_length=1,
            max_length=2,
        )
        super().__init__(
            title="Оценить работу",
            custom_id=f"task_review:{task_id}:{reviewer_id}",
            components=[
                disnake.ui.Label("Оценить скорость", speed),
                disnake.ui.Label("Оценить работу", quality),
            ],
            timeout=600,
        )

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        if inter.author.id != self.reviewer_id or inter.guild_id != self.guild_id:
            await inter.response.send_message(
                "Это меню создано для другого пользователя или сервера.", ephemeral=True
            )
            return
        if not isinstance(inter.author, disnake.Member):
            await inter.response.send_message(
                "Не удалось проверить роль лидера.", ephemeral=True
            )
            return

        task = store.get_task(self.guild_id, self.task_id)
        room = store.get_task_room(self.guild_id, self.task_id)
        if task is None or room is None or room["status"] != "ready_for_review":
            await inter.response.send_message(
                "Задание уже оценено или закрыто.", ephemeral=True
            )
            return
        if not {role.id for role in inter.author.roles}.intersection(
            task_leader_role_ids(task["role_ids"])
        ):
            await inter.response.send_message(
                "Оценить работу может только лидер нужного направления.", ephemeral=True
            )
            return

        raw_speed = inter.text_values.get("task_review:speed", "").strip()
        raw_quality = inter.text_values.get("task_review:quality", "").strip()
        if not raw_speed.isdecimal() or not raw_quality.isdecimal():
            await inter.response.send_message(
                "Обе оценки должны быть целыми числами от 1 до 10.", ephemeral=True
            )
            return
        speed_rating, quality_rating = int(raw_speed), int(raw_quality)
        if not 1 <= speed_rating <= 10 or not 1 <= quality_rating <= 10:
            await inter.response.send_message(
                "Обе оценки должны быть от 1 до 10.", ephemeral=True
            )
            return

        participants = [
            (item["user_id"], task_progression_track(task["role_ids"], item["role_ids"]))
            for item in store.list_applications(self.guild_id, self.task_id)
            if item["status"] == "approved"
        ]
        await inter.response.defer(ephemeral=True)
        completed = store.complete_task(
            guild_id=self.guild_id,
            task_id=self.task_id,
            reviewer_id=inter.author.id,
            speed_rating=speed_rating,
            quality_rating=quality_rating,
            reviewed_at=local_now().isoformat(),
            participants=participants,
        )
        if not completed:
            await inter.followup.send(
                "Не удалось сохранить оценку: задание уже обработано.", ephemeral=True
            )
            return

        promotions = await self.bot.apply_task_promotions(inter.guild, participants)
        await send_task_log(
            self.bot,
            inter.guild,
            title="Задание оценено",
            description=f"Задание ``#{self.task_id}`` — **{task['title']}**.",
            actor=inter.author.display_name,
            fields=[
                ("Скорость", f"{speed_rating}/10"),
                ("Качество работы", f"{quality_rating}/10"),
                ("Участников", str(len(participants))),
                ("Повышения", "\n".join(promotions) if promotions else "Нет"),
            ],
        )
        room_delete_errors = await self.bot.delete_task_room_resources(room)
        result = (
            f"Оценка сохранена: скорость — **{speed_rating}/10**, работа — "
            f"**{quality_rating}/10**. Категория задания закрыта."
        )
        if promotions:
            result += "\n" + "\n".join(promotions)
        if room_delete_errors:
            result += (
                "\nНе удалось удалить все каналы категории; проверьте права бота на "
                "управление каналами."
            )
        await inter.followup.send(result, ephemeral=True, allowed_mentions=ALLOWED_MENTIONS)


class TaskDeleteConfirmationView(disnake.ui.View):
    def __init__(self, bot: commands.InteractionBot, guild_id: int, task_id: int, user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.guild_id = guild_id
        self.task_id = task_id
        self.user_id = user_id

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.user_id:
            await inter.response.send_message(
                "Это подтверждение создано для другого пользователя.", ephemeral=True
            )
            return False
        return True

    @disnake.ui.button(label="Удалить задание", style=disnake.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        if (
            inter.guild is None
            or inter.guild.id != self.guild_id
            or not isinstance(inter.author, disnake.Member)
        ):
            await inter.response.send_message(
                "Удаление доступно только на исходном сервере.", ephemeral=True
            )
            return
        if not member_is_task_creator(inter.author):
            await inter.response.send_message(
                "У вас больше нет прав удалять задания.", ephemeral=True
            )
            return

        task = store.get_task(self.guild_id, self.task_id)
        room = store.get_task_room(self.guild_id, self.task_id)
        application_message_ids = store.delete_task(self.guild_id, self.task_id)
        if application_message_ids is None:
            await inter.response.edit_message(
                content=f"Задание **#{self.task_id}** уже удалено.",
                embed=None,
                attachments=[],
                view=None,
            )
            return

        await inter.response.edit_message(
            content=f"Удаляю задание **#{self.task_id}** и связанные заявки…",
            embed=None,
            attachments=[],
            view=None,
        )

        failed_messages = await self._delete_application_messages(application_message_ids)
        failed_channels = await self.bot.delete_task_room_resources(room)
        await send_task_log(
            self.bot,
            inter.guild,
            title="Задание удалено",
            description=(
                f"Задание ``#{self.task_id}`` — "
                f"**{task['title'] if task else 'неизвестное задание'}**."
            ),
            actor=inter.author.display_name,
            fields=[
                ("Удалено заявок", str(len(application_message_ids))),
                ("Ошибок удаления сообщений", str(failed_messages)),
                ("Ошибок удаления каналов", str(failed_channels)),
            ],
        )
        result = (
            f"Задание **#{self.task_id}** удалено. Удалено связанных заявок: "
            f"``{len(application_message_ids)}``."
        )
        if failed_messages:
            result += (
                f"\nНе удалось удалить ``{failed_messages}`` сообщений из канала заявок. "
                "Проверьте права бота на управление сообщениями."
            )
        if failed_channels:
            result += (
                "\nНе удалось удалить все каналы категории; проверьте права бота на "
                "управление каналами."
            )
        await inter.followup.send(result, ephemeral=True, allowed_mentions=ALLOWED_MENTIONS)

    async def _delete_application_messages(self, message_ids: list[int]) -> int:
        if not message_ids:
            return 0

        channel = self.bot.get_channel(APPLICATIONS_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(APPLICATIONS_CHANNEL_ID)
            except disnake.HTTPException:
                logger.exception(
                    "Could not fetch the applications channel while deleting task #%s",
                    self.task_id,
                )
                return len(message_ids)
        if not isinstance(channel, disnake.TextChannel) or channel.guild.id != self.guild_id:
            logger.error(
                "Applications channel is unavailable while deleting task #%s", self.task_id
            )
            return len(message_ids)

        failed = 0
        for message_id in message_ids:
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
            except disnake.NotFound:
                continue
            except disnake.HTTPException:
                failed += 1
                logger.exception("Could not delete application message %s", message_id)
        return failed

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary)
    async def cancel_delete(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        await inter.response.edit_message(
            content="Удаление задания отменено.",
            embed=None,
            attachments=[],
            view=None,
        )


class ApplicationDecisionView(disnake.ui.View):
    def __init__(self, bot: commands.InteractionBot):
        super().__init__(timeout=None)
        self.add_item(ApplicationDecisionSelect(bot))


class ApplicationDecisionSelect(disnake.ui.StringSelect):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        super().__init__(
            custom_id="task-application:decision",
            placeholder="Выберите решение по заявке",
            min_values=1,
            max_values=1,
            options=[
                disnake.SelectOption(label="Подтвердить", value="approved", emoji="✅"),
                disnake.SelectOption(label="Отклонить", value="rejected", emoji="❌"),
            ],
        )

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        await handle_application_decision(inter, self.values[0], self.bot)


async def handle_application_decision(
    inter: disnake.MessageInteraction,
    decision: str,
    bot: commands.InteractionBot,
) -> None:
    try:
        await inter.response.defer()
    except disnake.HTTPException:
        logger.warning("Could not acknowledge application decision %s", inter.id)
        return

    if inter.guild is None or not isinstance(inter.author, disnake.Member):
        await inter.followup.send("Решение можно принять только на сервере.", ephemeral=True)
        return

    application = store.get_application_by_message(inter.guild.id, inter.message.id)
    if application is None:
        await inter.followup.send("Заявка не найдена или уже удалена.", ephemeral=True)
        return
    if application["status"] != "pending":
        status_text = {"approved": "подтверждена", "rejected": "отклонена"}.get(
            application["status"], application["status"]
        )
        await inter.followup.send(f"Эта заявка уже {status_text}.", ephemeral=True)
        return

    task = store.get_task(inter.guild.id, application["task_id"])
    if task is None:
        await inter.followup.send("Задание для этой заявки не найдено.", ephemeral=True)
        return

    room = store.get_task_room(inter.guild.id, task["id"])
    if decision == "approved" and room is not None and room["status"] != "active":
        await inter.followup.send(
            "К этому заданию больше нельзя добавлять участников: оно уже отправлено на проверку.",
            ephemeral=True,
        )
        return

    allowed_roles = application_approver_role_ids(task["role_ids"])
    member_roles = {role.id for role in inter.author.roles}
    if not member_roles.intersection(allowed_roles):
        mentions = (
            ", ".join(f"<@&{role_id}>" for role_id in sorted(allowed_roles))
            or "нет назначенных ролей"
        )
        await inter.followup.send(
            f"Решение по этой заявке могут принять: {mentions}.",
            ephemeral=True,
            allowed_mentions=ALLOWED_MENTIONS,
        )
        return

    if not store.decide_application(inter.guild.id, inter.message.id, decision):
        await inter.followup.send(
            "Заявка уже обработана другим участником.", ephemeral=True
        )
        return

    decision_text = "Подтверждена" if decision == "approved" else "Отклонена"
    embed = (
        inter.message.embeds[0].copy()
        if inter.message.embeds
        else disnake.Embed(color=EMBED_COLOR)
    )
    embed.color = disnake.Colour.green() if decision == "approved" else disnake.Colour.red()
    embed.add_field(
        name="Решение",
        value=code_block(
            f"{decision_text}\n{inter.author.display_name}\n{local_now():%d.%m.%Y, %H:%M}", 300
        ),
        inline=False,
    )
    await inter.edit_original_response(
        embed=embed,
        attachments=inter.message.attachments,
        view=None,
    )
    if decision == "approved" and room is not None:
        if not await bot.add_task_participant(inter.guild, task, application["user_id"]):
            await inter.followup.send(
                "Отклик подтверждён, но бот не смог открыть участнику каналы задания. "
                "Проверьте права бота.",
                ephemeral=True,
            )
    await send_task_log(
        bot,
        inter.guild,
        title=f"Заявка {decision_text.lower()}",
        description=f"Заявка на задание ``#{task['id']}`` от <@{application['user_id']}>.",
        actor=inter.author.display_name,
        fields=[("Название задания", task["title"]), ("Решение", decision_text)],
    )
