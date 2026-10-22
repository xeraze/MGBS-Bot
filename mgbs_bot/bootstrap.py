from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import socket
import tempfile
from logging.handlers import RotatingFileHandler

import disnake
from dotenv import load_dotenv
from disnake.ext import commands

PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGE_DIR.parent
load_dotenv(BASE_DIR / ".env")

LOG_DIR = BASE_DIR / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_DIR / "bot.log",
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("task_bot")

_instance_lock_handle = None


def acquire_instance_lock(token: str) -> None:
    global _instance_lock_handle
    token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / f"mgbs-bot-{token_fingerprint}.lock"
    _instance_lock_handle = lock_path.open("a+b")
    _instance_lock_handle.seek(0, os.SEEK_END)
    if _instance_lock_handle.tell() == 0:
        _instance_lock_handle.write(b"\0")
        _instance_lock_handle.flush()
    _instance_lock_handle.seek(0)

    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(_instance_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(_instance_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        _instance_lock_handle.close()
        _instance_lock_handle = None
        raise SystemExit(
            "MGBS Bot с этим Discord-токеном уже запущен. Остановите предыдущий "
            "экземпляр, в том числе запущенный из другой папки."
        ) from exc


def create_bot() -> commands.InteractionBot:
    from mgbs_bot.bot import TaskBot

    sync_flags = commands.CommandSyncFlags.default()
    sync_flags.sync_commands_debug = os.getenv("DISCORD_DEBUG", "").strip() == "1"
    test_guild_id = os.getenv("DISCORD_TEST_GUILD_ID", "").strip()
    test_guilds = [int(test_guild_id)] if test_guild_id.isdecimal() else None
    intents = disnake.Intents.default()
    intents.members = True
    return TaskBot(
        intents=intents,
        command_sync_flags=sync_flags,
        test_guilds=test_guilds,
    )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Не задан DISCORD_TOKEN. Создайте .env по примеру в .env.example "
            "или задайте переменную окружения."
        )
    logger.info(
        "Starting bot process: pid=%s, host=%s, project=%s",
        os.getpid(),
        socket.gethostname(),
        BASE_DIR,
    )
    acquire_instance_lock(token)
    create_bot().run(token)
