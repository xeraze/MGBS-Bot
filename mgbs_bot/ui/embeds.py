from __future__ import annotations

import disnake

from mgbs_bot.config import BANNER_PATH, EMBED_COLOR
from mgbs_bot.utils import code_block, footer_text, task_roles_text


def make_panel_embed(*, include_banner: bool = True) -> disnake.Embed:
    embed = disnake.Embed(
        title="Задания команды",
        description=(
            "Откройте список заданий кнопкой ниже.\n\n"
            "• **Просмотр** — постранично, одно задание на странице.\n"
            "• **Взять задание** — кнопка на странице задания.\n\n"
            "Отклик рассмотрит руководитель нужного направления."
        ),
        color=EMBED_COLOR,
    )
    if include_banner and BANNER_PATH.is_file():
        embed.set_image(url="attachment://banner.png")
    return embed


def build_task_page(
    *,
    task: dict,
    applications: list[dict],
    page_number: int,
    page_total: int,
    username: str,
    avatar_url: str | None,
) -> disnake.Embed:
    status_names = {"pending": "ожидает", "approved": "принят", "rejected": "отклонён"}
    applicant_lines = [
        f"{item['username']} · {item['user_id']} · {status_names.get(item['status'], item['status'])}"
        for item in applications
    ]
    applicants_text = "\n".join(applicant_lines) if applicant_lines else "Пока нет откликов"

    embed = disnake.Embed(
        title=f"Задание #{task['id']} · {task['title']}"[:256],
        description=(
            f"Страница ``{page_number}/{page_total}`` · Открытых заданий: ``{page_total}``"
        ),
        color=EMBED_COLOR,
    )
    embed.add_field(name="🎭 Роли", value=code_block(task_roles_text(task["role_ids"]), 950), inline=False)
    embed.add_field(name="📝 Что нужно сделать", value=code_block(task["description"], 950), inline=False)
    embed.add_field(name="⏳ Дедлайн", value=code_block(task["deadline"], 200), inline=False)
    embed.add_field(name="⚙️ Сложность", value=code_block(task["difficulty"], 100), inline=False)
    embed.add_field(name="📨 Количество откликов", value=code_block(len(applications), 80), inline=False)
    embed.add_field(name="👥 Кто откликнулся", value=code_block(applicants_text, 950), inline=False)
    embed.set_footer(text=footer_text(username), icon_url=avatar_url)
    if BANNER_PATH.is_file():
        embed.set_image(url="attachment://banner.png")
    return embed


def build_empty_tasks_page(*, username: str, avatar_url: str | None) -> disnake.Embed:
    embed = disnake.Embed(
        title="Задания команды",
        description="Пока нет открытых заданий.",
        color=EMBED_COLOR,
    )
    embed.set_footer(text=footer_text(username), icon_url=avatar_url)
    if BANNER_PATH.is_file():
        embed.set_image(url="attachment://banner.png")
    return embed
