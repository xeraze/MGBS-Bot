from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class TaskStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS panels (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS panel_cleanup (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, channel_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT 'Задание',
                    description TEXT NOT NULL,
                    deadline TEXT NOT NULL,
                    difficulty TEXT NOT NULL DEFAULT 'Средняя',
                    role_ids TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    is_open INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    role_ids TEXT NOT NULL,
                    joined_at TEXT,
                    submitted_at TEXT NOT NULL,
                    message_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE(task_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS task_rooms (
                    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                    guild_id INTEGER NOT NULL,
                    category_id INTEGER NOT NULL,
                    info_channel_id INTEGER NOT NULL,
                    general_channel_id INTEGER NOT NULL,
                    voice_channel_id INTEGER NOT NULL,
                    info_message_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'active'
                );

                CREATE TABLE IF NOT EXISTS task_room_progress (
                    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                    guild_id INTEGER NOT NULL,
                    category_id INTEGER,
                    info_channel_id INTEGER,
                    general_channel_id INTEGER,
                    voice_channel_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS task_reviews (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    track TEXT,
                    speed_rating INTEGER NOT NULL,
                    quality_rating INTEGER NOT NULL,
                    reviewer_id INTEGER NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS member_activity (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    last_active_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS task_reminders (
                    guild_id INTEGER NOT NULL,
                    task_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    last_ping_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, task_id, user_id)
                );
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_applications_guild_task ON applications(guild_id, task_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_guild_open ON tasks(guild_id, is_open)"
            )
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        panel_columns = {row["name"] for row in connection.execute("PRAGMA table_info(panels)")}
        if "version" not in panel_columns:
            connection.execute("ALTER TABLE panels ADD COLUMN version INTEGER NOT NULL DEFAULT 1")

        reminder_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(task_reminders)")
        }
        if "role_id" in reminder_columns and "user_id" not in reminder_columns:
            connection.execute("ALTER TABLE task_reminders RENAME COLUMN role_id TO user_id")

        task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
        if "title" not in task_columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN title TEXT NOT NULL DEFAULT 'Задание'")
            connection.execute(
                """UPDATE tasks SET title = TRIM(REPLACE(REPLACE(
                       SUBSTR(description, 1, 80), CHAR(10), ' '), CHAR(13), ' '))"""
            )
        if "difficulty" not in task_columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN difficulty TEXT NOT NULL DEFAULT 'Средняя'")

    @staticmethod
    def _task(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["role_ids"] = json.loads(item["role_ids"])
        item["is_open"] = bool(item["is_open"])
        return item

    @staticmethod
    def _application(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["role_ids"] = json.loads(item["role_ids"])
        return item

    def rotate_panel(
        self, guild_id: int, channel_id: int, message_id: int, version: int = 1
    ) -> None:
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT channel_id, message_id FROM panels WHERE guild_id = ?", (guild_id,)
            ).fetchone()
            if previous is not None and (
                previous["channel_id"] != channel_id or previous["message_id"] != message_id
            ):
                connection.execute(
                    "INSERT OR IGNORE INTO panel_cleanup(guild_id, channel_id, message_id) VALUES (?, ?, ?)",
                    (guild_id, previous["channel_id"], previous["message_id"]),
                )
            connection.execute(
                """INSERT INTO panels(guild_id, channel_id, message_id, version) VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                   channel_id = excluded.channel_id, message_id = excluded.message_id,
                   version = excluded.version""",
                (guild_id, channel_id, message_id, version),
            )

    def get_panel_cleanup(self, guild_id: int) -> list[dict[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT channel_id, message_id FROM panel_cleanup WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_panel_cleanup(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM panel_cleanup WHERE guild_id = ? AND channel_id = ? AND message_id = ?",
                (guild_id, channel_id, message_id),
            )

    def get_panel(self, guild_id: int) -> dict[str, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT channel_id, message_id, version FROM panels WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_task(
        self,
        *,
        guild_id: int,
        title: str,
        description: str,
        deadline: str,
        difficulty: str,
        role_ids: list[int],
        created_by: int,
        created_at: str,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO tasks
                   (guild_id, title, description, deadline, difficulty, role_ids, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    title,
                    description,
                    deadline,
                    difficulty,
                    json.dumps(role_ids),
                    created_by,
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def save_task_rooms(
        self,
        *,
        task_id: int,
        guild_id: int,
        category_id: int,
        info_channel_id: int,
        general_channel_id: int,
        voice_channel_id: int,
        info_message_id: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO task_rooms
                   (task_id, guild_id, category_id, info_channel_id, general_channel_id,
                    voice_channel_id, info_message_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
                   ON CONFLICT(task_id) DO UPDATE SET
                   guild_id = excluded.guild_id,
                   category_id = excluded.category_id,
                   info_channel_id = excluded.info_channel_id,
                   general_channel_id = excluded.general_channel_id,
                   voice_channel_id = excluded.voice_channel_id,
                   info_message_id = excluded.info_message_id,
                   status = 'active'""",
                (
                    task_id,
                    guild_id,
                    category_id,
                    info_channel_id,
                    general_channel_id,
                    voice_channel_id,
                    info_message_id,
                ),
            )
            connection.execute("DELETE FROM task_room_progress WHERE task_id = ?", (task_id,))

    def save_task_room_progress(
        self,
        *,
        task_id: int,
        guild_id: int,
        category_id: int | None,
        info_channel_id: int | None,
        general_channel_id: int | None,
        voice_channel_id: int | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO task_room_progress
                   (task_id, guild_id, category_id, info_channel_id, general_channel_id, voice_channel_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                   guild_id = excluded.guild_id,
                   category_id = excluded.category_id,
                   info_channel_id = excluded.info_channel_id,
                   general_channel_id = excluded.general_channel_id,
                   voice_channel_id = excluded.voice_channel_id""",
                (task_id, guild_id, category_id, info_channel_id, general_channel_id, voice_channel_id),
            )

    def get_task_room_progress(self, task_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_room_progress WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_task_info_message(self, task_id: int, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE task_rooms SET info_message_id = ? WHERE task_id = ?",
                (message_id, task_id),
            )

    def get_task_room(self, guild_id: int, task_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_rooms WHERE guild_id = ? AND task_id = ?",
                (guild_id, task_id),
            ).fetchone()
        return dict(row) if row else None

    def list_active_task_rooms(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT t.*, r.category_id, r.info_channel_id, r.general_channel_id,
                          r.voice_channel_id, r.info_message_id, r.status AS room_status
                   FROM tasks AS t JOIN task_rooms AS r ON r.task_id = t.id
                   WHERE r.status IN ('active', 'ready_for_review')
                   ORDER BY t.id"""
            ).fetchall()
        return [self._task(row) for row in rows if row is not None]

    def set_task_room_status(
        self,
        guild_id: int,
        task_id: int,
        expected_status: str,
        new_status: str,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE task_rooms SET status = ?
                   WHERE guild_id = ? AND task_id = ? AND status = ?""",
                (new_status, guild_id, task_id, expected_status),
            )
        return cursor.rowcount == 1

    def complete_task(
        self,
        *,
        guild_id: int,
        task_id: int,
        reviewer_id: int,
        speed_rating: int,
        quality_rating: int,
        reviewed_at: str,
        participants: list[tuple[int, str | None]],
    ) -> bool:
        with self._connect() as connection:
            room = connection.execute(
                "SELECT status FROM task_rooms WHERE guild_id = ? AND task_id = ?",
                (guild_id, task_id),
            ).fetchone()
            if room is None or room["status"] != "ready_for_review":
                return False

            connection.executemany(
                """INSERT INTO task_reviews
                   (task_id, guild_id, user_id, track, speed_rating, quality_rating, reviewer_id, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (task_id, guild_id, user_id, track, speed_rating, quality_rating, reviewer_id, reviewed_at)
                    for user_id, track in participants
                ],
            )
            connection.execute(
                "UPDATE tasks SET is_open = 0 WHERE guild_id = ? AND id = ?",
                (guild_id, task_id),
            )
            connection.execute(
                "UPDATE task_rooms SET status = 'completed' WHERE guild_id = ? AND task_id = ?",
                (guild_id, task_id),
            )
        return True

    def get_task_progress(self, guild_id: int, user_id: int, track: str) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS completed,
                          SUM(CASE WHEN quality_rating >= 7 THEN 1 ELSE 0 END) AS rated_7,
                          SUM(CASE WHEN quality_rating >= 9 THEN 1 ELSE 0 END) AS rated_9
                   FROM task_reviews WHERE guild_id = ? AND user_id = ? AND track = ?""",
                (guild_id, user_id, track),
            ).fetchone()
        return {
            "completed": int(row["completed"] or 0),
            "rated_7": int(row["rated_7"] or 0),
            "rated_9": int(row["rated_9"] or 0),
        }

    def get_task(self, guild_id: int, task_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE guild_id = ? AND id = ?",
                (guild_id, task_id),
            ).fetchone()
        return self._task(row)

    def delete_task(self, guild_id: int, task_id: int) -> list[int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM tasks WHERE guild_id = ? AND id = ?",
                (guild_id, task_id),
            ).fetchone()
            if row is None:
                return None

            message_rows = connection.execute(
                "SELECT message_id FROM applications WHERE guild_id = ? AND task_id = ?",
                (guild_id, task_id),
            ).fetchall()
            connection.execute(
                "DELETE FROM tasks WHERE guild_id = ? AND id = ?",
                (guild_id, task_id),
            )
        return [int(row["message_id"]) for row in message_rows if row["message_id"] is not None]

    def list_tasks(self, guild_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE guild_id = ? AND is_open = 1 ORDER BY id",
                (guild_id,),
            ).fetchall()
        return [self._task(row) for row in rows if row is not None]

    def list_applications(self, guild_id: int, task_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM applications
                   WHERE guild_id = ? AND task_id = ? ORDER BY submitted_at""",
                (guild_id, task_id),
            ).fetchall()
        return [self._application(row) for row in rows]

    def create_application(
        self,
        *,
        task_id: int,
        guild_id: int,
        user_id: int,
        username: str,
        role_ids: list[int],
        joined_at: str | None,
        submitted_at: str,
    ) -> bool:
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO applications
                       (task_id, guild_id, user_id, username, role_ids, joined_at, submitted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task_id,
                        guild_id,
                        user_id,
                        username,
                        json.dumps(role_ids),
                        joined_at,
                        submitted_at,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def set_application_message(self, task_id: int, user_id: int, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE applications SET message_id = ? WHERE task_id = ? AND user_id = ?",
                (message_id, task_id, user_id),
            )

    def get_application_by_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM applications WHERE guild_id = ? AND message_id = ?",
                (guild_id, message_id),
            ).fetchone()
        return self._application(row) if row else None

    def list_pending_applications(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM applications
                   WHERE status = 'pending' AND message_id IS NOT NULL
                   ORDER BY submitted_at"""
            ).fetchall()
        return [self._application(row) for row in rows]

    def decide_application(self, guild_id: int, message_id: int, status: str) -> bool:
        if status not in {"approved", "rejected"}:
            raise ValueError("Application status must be approved or rejected")
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE applications SET status = ?
                   WHERE guild_id = ? AND message_id = ? AND status = 'pending'""",
                (status, guild_id, message_id),
            )
        return cursor.rowcount == 1

    def delete_application(self, task_id: int, user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM applications WHERE task_id = ? AND user_id = ?",
                (task_id, user_id),
            )

    def list_applications_for_guild(self, guild_id: int) -> dict[int, list[dict[str, Any]]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM applications
                   WHERE guild_id = ? ORDER BY submitted_at""",
                (guild_id,),
            ).fetchall()
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            item = self._application(row)
            grouped.setdefault(item["task_id"], []).append(item)
        return grouped

    def touch_member_activity(self, guild_id: int, user_id: int, timestamp: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO member_activity(guild_id, user_id, last_active_at) VALUES (?, ?, ?)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET
                   last_active_at = excluded.last_active_at""",
                (guild_id, user_id, timestamp),
            )

    def list_member_activity(self, guild_id: int) -> dict[int, str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT user_id, last_active_at FROM member_activity WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
        return {int(row["user_id"]): row["last_active_at"] for row in rows}

    def get_last_task_ping(self, guild_id: int, task_id: int, user_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT last_ping_at FROM task_reminders
                   WHERE guild_id = ? AND task_id = ? AND user_id = ?""",
                (guild_id, task_id, user_id),
            ).fetchone()
        return row["last_ping_at"] if row else None

    def record_task_ping(self, guild_id: int, task_id: int, user_id: int, timestamp: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO task_reminders(guild_id, task_id, user_id, last_ping_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, task_id, user_id) DO UPDATE SET
                   last_ping_at = excluded.last_ping_at""",
                (guild_id, task_id, user_id, timestamp),
            )
