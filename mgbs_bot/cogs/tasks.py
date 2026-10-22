from __future__ import annotations

import disnake
from disnake.ext import commands

from mgbs_bot.config import EMBED_COLOR
from mgbs_bot.ui.tasks import TaskCreateModal, TaskDeleteConfirmationView
from mgbs_bot.utils import (
    ALLOWED_MENTIONS,
    add_banner,
    code_block,
    footer_text,
    logger,
    member_is_task_creator,
    store,
    task_roles_text,
)


class TaskCog(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(name="task", description="Управление заданиями команды")
    async def task(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @task.sub_command(name="create", description="Создать задание для выбранных ролей")
    async def task_create(self, inter: disnake.ApplicationCommandInteraction) -> None:
        if inter.guild is None or not isinstance(inter.author, disnake.Member):
            await inter.response.send_message(
                "Команда доступна только на сервере.", ephemeral=True
            )
            return
        if not member_is_task_creator(inter.author):
            await inter.response.send_message(
                "Создавать задания могут только администраторы.",
                ephemeral=True,
            )
            return

        try:
            await inter.response.send_modal(TaskCreateModal(self.bot, inter.author.id))
        except disnake.HTTPException:
            logger.exception("Could not open task-create modal: %s", inter.id)
            try:
                await inter.followup.send(
                    "Не удалось открыть форму создания. Повторите попытку.",
                    ephemeral=True,
                )
            except disnake.HTTPException:
                logger.debug("Fallback notice failed for interaction %s", inter.id)

    @task.sub_command(name="delete", description="Удалить задание по его номеру")
    async def task_delete(
        self,
        inter: disnake.ApplicationCommandInteraction,
        task_number: int = commands.Param(description="Номер задания из списка Tasks"),
    ) -> None:
        if inter.guild is None or not isinstance(inter.author, disnake.Member):
            await inter.response.send_message(
                "Команда доступна только на сервере.", ephemeral=True
            )
            return
        if not member_is_task_creator(inter.author):
            await inter.response.send_message(
                "Удалять задания могут только администраторы.",
                ephemeral=True,
            )
            return

        task = store.get_task(inter.guild.id, task_number)
        if task is None:
            await inter.response.send_message(
                f"Задание **#{task_number}** не найдено.", ephemeral=True
            )
            return

        try:
            await inter.response.defer(ephemeral=True)
        except disnake.HTTPException:
            logger.warning("Discord task-delete interaction was expired or acknowledged")
            return

        embed = disnake.Embed(
            title="Подтвердите удаление",
            description=(
                f"Задание **#{task['id']}** и все связанные заявки будут удалены."
            ),
            color=EMBED_COLOR,
        )
        embed.add_field(
            name="🎭 Роли", value=code_block(task_roles_text(task["role_ids"])), inline=False
        )
        embed.add_field(name="📝 Задание", value=code_block(task["description"]), inline=False)
        embed.add_field(name="⏳ Дедлайн", value=code_block(task["deadline"], 200), inline=False)
        embed.set_footer(
            text=footer_text(inter.author.display_name), icon_url=inter.author.display_avatar.url
        )
        banner = add_banner(embed)
        options: dict = {
            "embed": embed,
            "view": TaskDeleteConfirmationView(
                self.bot, inter.guild.id, task["id"], inter.author.id
            ),
            "allowed_mentions": ALLOWED_MENTIONS,
        }
        if banner is not None:
            options["file"] = banner
        await inter.edit_original_response(**options)
