from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGE_DIR.parent
BANNER_PATH = PACKAGE_DIR / "banner.png"
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATABASE_PATH = Path(os.getenv("TASK_BOT_DATABASE", DATA_DIR / "tasks.sqlite3"))

APPLICATIONS_CHANNEL_ID = 1558827559809196112
TASK_LOG_CHANNEL_ID = 1558917037454000288
EMBED_COLOR = 0x333333
TIMEZONE = "Europe/Prague"

PANEL_VERSION = 3


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdecimal() else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


AFK_PING_CHANNEL_ID = _env_int("AFK_PING_CHANNEL_ID", 1562480044713181237)
AFK_HOURS = _env_float("AFK_HOURS", 24.0)
AFK_SCAN_INTERVAL_MINUTES = _env_float("AFK_SCAN_INTERVAL_MINUTES", 15.0)
AFK_REMINDER_COOLDOWN_HOURS = _env_float("AFK_REMINDER_COOLDOWN_HOURS", 12.0)


BUILDER_ROLES = (
    1549023935453204490,
    1549488943387516939,
    1549022396818788392,
    1551219746509164715,
)
MODELLER_ROLES = (
    1549023935453204490,
    1549488672754110515,
    1549022846699970690,
    1549802898727112794,
)
SCRIPTER_ROLES = (
    1549024194577170452,
    1549350270096384070,
    1549022984264622102,
    1553817878703120504,
)
SPECIALIST_ROLES = (
    1549023180515971133,
    1549084093864484994,
    1553823964680290305,
    1549024587550040175,
    1558139368432996422,
)

ROLE_NAMES = {
    1549023935453204490: "Главный билдер / главный моделлер",
    1549488943387516939: "Старший билдер",
    1549022396818788392: "Билдер",
    1551219746509164715: "Младший билдер",
    1549488672754110515: "Старший моделлер",
    1549022846699970690: "Моделлер",
    1549802898727112794: "Младший моделлер",
    1549024194577170452: "Главный скриптер",
    1549350270096384070: "Старший скриптер",
    1549022984264622102: "Скриптер",
    1553817878703120504: "Младший скриптер",
    1549023180515971133: "UI-дизайнер",
    1549084093864484994: "SFX-продюсер",
    1553823964680290305: "Младший дизайнер",
    1549024587550040175: "Аниматор",
    1558139368432996422: "Младший аниматор",
}

TASK_ROLE_IDS = tuple(ROLE_NAMES)
TASK_CREATOR_ROLE_IDS = frozenset(
    {
        1549023935453204490,
        1549024194577170452,
        1553338919817584700,
        1548661965659054090,
        1549083792126513275,
    }
)
SPECIAL_TASK_APPROVER_ROLE_IDS = frozenset(
    {
        1553338919817584700,
        1548661965659054090,
        1549083792126513275,
    }
)
