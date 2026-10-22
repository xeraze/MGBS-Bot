from __future__ import annotations

import disnake
from disnake.ext import commands

from mgbs_bot.config import BANNER_PATH
from mgbs_bot.services import submit_application
from mgbs_bot.ui.embeds import build_empty_tasks_page, build_task_page
from mgbs_bot.utils import (
    ALLOWED_MENTIONS,
    eligible_task_roles,
    logger,
    make_banner_file,
    store,
)

PANEL_OPEN_CUSTOM_ID = "task-panel:open"
TASK_TAKE_CUSTOM_ID = "task-panel:take"
TASK_PAGE_SELECT_CUSTOM_ID = "task-panel:page"
PAGE_WINDOW = 5


class OpenTasksButton(disnake.ui.Button):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        super().__init__(
            custom_id=PANEL_OPEN_CUSTOM_ID,
            label="Задания",
            emoji="📋",
            style=disnake.ButtonStyle.primary,
        )

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        await open_task_browser(self.bot, inter)


class PersistentTaskPanelView(disnake.ui.View):
    def __init__(self, bot: commands.InteractionBot):
        super().__init__(timeout=None)
        self.add_item(OpenTasksButton(bot))


class TakeTaskButton(disnake.ui.Button):
    def __init__(self, browser: TaskBrowserView):
        self.browser = browser
        super().__init__(custom_id=TASK_TAKE_CUSTOM_ID, row=0)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        await self.browser.take_current_task(inter)


class PageSelect(disnake.ui.StringSelect):
    def __init__(self, browser: TaskBrowserView):
        self.browser = browser
        super().__init__(
            custom_id=TASK_PAGE_SELECT_CUSTOM_ID,
            placeholder="Навигация по заданиям",
            min_values=1,
            max_values=1,
            options=browser.nav_options(),
            row=1,
        )

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        await self.browser.handle_page_value(inter, self.values[0])


class TaskBrowserView(disnake.ui.View):
    def __init__(
        self,
        bot: commands.InteractionBot,
        *,
        tasks: list[dict],
        applications_by_task: dict[int, list[dict]],
        user_id: int,
        username: str,
        avatar_url: str | None,
        eligible_role_ids: set[int],
        applied_task_ids: set[int],
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.tasks = tasks
        self.applications_by_task = applications_by_task
        self.user_id = user_id
        self.username = username
        self.avatar_url = avatar_url
        self.eligible_role_ids = set(eligible_role_ids)
        self.applied_task_ids = set(applied_task_ids)
        self.page_index = 0
        self.take_button = TakeTaskButton(self)
        self.rebuild()

    @property
    def current_task(self) -> dict | None:
        if not self.tasks:
            return None
        return self.tasks[self.page_index]

    def sync_take_state(self) -> None:
        task = self.current_task
        if task is None:
            self.take_button.disabled = True
            return
        if task["id"] in self.applied_task_ids:
            self.take_button.label = "Заявка отправлена"
            self.take_button.emoji = "✅"
            self.take_button.style = disnake.ButtonStyle.success
            self.take_button.disabled = True
        elif not self.eligible_role_ids.intersection(task["role_ids"]):
            self.take_button.label = "Нет подходящей роли"
            self.take_button.emoji = "🔒"
            self.take_button.style = disnake.ButtonStyle.secondary
            self.take_button.disabled = True
        else:
            self.take_button.label = "Взять задание"
            self.take_button.emoji = "✅"
            self.take_button.style = disnake.ButtonStyle.primary
            self.take_button.disabled = False

    def mark_applied(self, task_id: int) -> None:
        self.applied_task_ids.add(task_id)
        self.sync_take_state()

    def nav_options(self) -> list[disnake.SelectOption]:
        total = len(self.tasks)
        index = self.page_index
        options: list[disnake.SelectOption] = []
        if index > 0:
            options.append(
                disnake.SelectOption(label="Назад", value="prev", emoji="◀️")
            )
        start = min(max(index - (PAGE_WINDOW // 2), 0), max(total - PAGE_WINDOW, 0))
        for page in range(start, min(start + PAGE_WINDOW, total)):
            options.append(
                disnake.SelectOption(
                    label=str(page + 1),
                    value=str(page),
                    default=page == index,
                )
            )
        if index < total - 1:
            options.append(
                disnake.SelectOption(label="Далее", value="next", emoji="▶️")
            )
        return options

    def rebuild(self) -> None:
        self.clear_items()
        if not self.tasks:
            return
        self.sync_take_state()
        self.add_item(self.take_button)
        self.add_item(PageSelect(self))

    def current_embed(self) -> disnake.Embed:
        task = self.current_task
        if task is None:
            return build_empty_tasks_page(
                username=self.username, avatar_url=self.avatar_url
            )
        return build_task_page(
            task=task,
            applications=self.applications_by_task.get(task["id"], []),
            page_number=self.page_index + 1,
            page_total=len(self.tasks),
            username=self.username,
            avatar_url=self.avatar_url,
        )

    def message_kwargs(self, *, attach_banner: bool = False) -> dict:
        kwargs = {
            "embeds": [self.current_embed()],
            "view": self,
            "allowed_mentions": ALLOWED_MENTIONS,
        }
        if attach_banner and BANNER_PATH.is_file():
            kwargs["files"] = [make_banner_file()]
            kwargs["attachments"] = []
        return kwargs

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self.user_id:
            return True
        await inter.response.send_message(
            "Это меню заданий открыто для другого пользователя.", ephemeral=True
        )
        return False

    async def handle_page_value(
        self, inter: disnake.MessageInteraction, value: str
    ) -> None:
        if value == "prev":
            target = self.page_index - 1
        elif value == "next":
            target = self.page_index + 1
        else:
            target = int(value)
        target = min(max(target, 0), len(self.tasks) - 1)
        if target == self.page_index:
            await inter.response.defer(with_message=False)
            return
        self.page_index = target
        self.rebuild()
        try:
            await inter.response.edit_message(**self.message_kwargs())
        except disnake.HTTPException:
            logger.exception(
                "Could not change task-list page: interaction_id=%s page=%s/%s",
                inter.id,
                self.page_index + 1,
                len(self.tasks),
            )

    async def take_current_task(self, inter: disnake.MessageInteraction) -> None:
        task = self.current_task
        if task is None or not task["is_open"]:
            await inter.response.send_message(
                "Это задание больше не открыто. Обновите список через панель.",
                ephemeral=True,
            )
            return
        await submit_application(self.bot, inter, task, browser=self)


async def open_task_browser(
    bot: commands.InteractionBot, inter: disnake.MessageInteraction
) -> None:
    if inter.guild is None:
        await inter.response.send_message(
            "Панель доступна только на сервере.", ephemeral=True
        )
        return

    panel = store.get_panel(inter.guild.id)
    if (
        panel is None
        or panel["message_id"] != inter.message.id
        or panel["channel_id"] != inter.channel_id
    ):
        await inter.response.send_message(
            "Эта панель уже не активна. Попросите администратора обновить её.",
            ephemeral=True,
        )
        return

    try:
        await inter.response.defer(with_message=True, ephemeral=True)
    except disnake.HTTPException as exc:
        if exc.code in {10062, 40060}:
            logger.warning(
                "Could not acknowledge task-panel interaction %s", inter.id
            )
            return
        raise

    try:
        tasks = store.list_tasks(inter.guild.id)
        applications = store.list_applications_for_guild(inter.guild.id)
        member = inter.author
        eligible = (
            eligible_task_roles(member) if isinstance(member, disnake.Member) else set()
        )
        applied = {
            task_id
            for task_id, items in applications.items()
            if any(item["user_id"] == inter.author.id for item in items)
        }
        view = TaskBrowserView(
            bot,
            tasks=tasks,
            applications_by_task=applications,
            user_id=inter.author.id,
            username=inter.author.display_name,
            avatar_url=inter.author.display_avatar.url,
            eligible_role_ids=eligible,
            applied_task_ids=applied,
        )
        await inter.edit_original_response(**view.message_kwargs(attach_banner=True))
        logger.info(
            "Task browser opened: guild_id=%s user_id=%s tasks=%s",
            inter.guild.id,
            inter.author.id,
            len(tasks),
        )
    except Exception:
        logger.exception(
            "Could not display task-panel content for interaction %s", inter.id
        )
        await inter.edit_original_response(
            content="Не удалось загрузить список заданий. Повторите попытку."
        )
