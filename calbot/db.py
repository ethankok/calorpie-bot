"""SQLite storage for users and meal entries."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    first_name    TEXT,
    authorized    INTEGER NOT NULL DEFAULT 0,
    goal_kcal     INTEGER,
    tz            TEXT NOT NULL DEFAULT 'Asia/Singapore',
    reminder_time TEXT,              -- 'HH:MM' local, NULL = off
    notes         TEXT NOT NULL DEFAULT '',  -- learned habits fed to every estimate
    code_attempts INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(user_id),
    eaten_at       TEXT NOT NULL,     -- UTC ISO-8601
    local_date     TEXT NOT NULL,     -- YYYY-MM-DD in the user's tz at eaten_at
    meal_type      TEXT NOT NULL,     -- breakfast/lunch/dinner/supper/snack
    title          TEXT NOT NULL,
    kcal           REAL NOT NULL,
    protein_g      REAL NOT NULL,
    carbs_g        REAL NOT NULL,
    fat_g          REAL NOT NULL,
    estimate_json  TEXT NOT NULL,     -- base Estimate (portion 1x) for refinement/audit
    portion        REAL NOT NULL DEFAULT 1.0,  -- multiplier applied to the base estimate
    source         TEXT NOT NULL,     -- photo/text/voice
    photo_file_ids TEXT,              -- JSON list of Telegram file_ids
    user_text      TEXT,
    bot_message_id INTEGER,
    dedupe_key     TEXT,              -- Telegram file_unique_ids, to catch re-sent photos
    deleted        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS meals_user_date ON meals(user_id, local_date) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS meals_bot_msg ON meals(user_id, bot_message_id);
-- Any bot message that refers to a meal (current card, retired cards, Fix prompts), so replies find it.
CREATE TABLE IF NOT EXISTS meal_links (
    user_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    meal_id    INTEGER NOT NULL REFERENCES meals(id),
    PRIMARY KEY (user_id, message_id)
);
CREATE TABLE IF NOT EXISTS api_usage (
    user_id    INTEGER NOT NULL,
    local_date TEXT NOT NULL,
    calls      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, local_date)
);
"""

MEAL_TYPES = ("breakfast", "lunch", "dinner", "supper", "snack")
SUPPER_MIN_KCAL = 400  # a late-night plate of noodles is supper; an apple is still a snack
# A "day" runs 04:00-04:00 local time, so a 1am supper counts towards the evening before.
DAY_START = timedelta(hours=4)


def log_date(local_dt: datetime) -> date:
    return (local_dt - DAY_START).date()


@dataclass
class User:
    user_id: int
    chat_id: int
    first_name: str | None
    authorized: bool
    goal_kcal: int | None
    tz: str
    reminder_time: str | None
    notes: str
    code_attempts: int = 0

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def today(self) -> date:
        return log_date(datetime.now(self.zone))


@dataclass
class Meal:
    id: int
    user_id: int
    eaten_at: datetime
    local_date: date
    meal_type: str
    title: str
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    estimate_json: str
    portion: float
    source: str
    photo_file_ids: list[str]
    user_text: str | None
    bot_message_id: int | None
    deleted: bool = False


def guess_meal_type(local_dt: datetime, kcal: float = 0) -> str:
    """Core windows, with the gaps between them split towards the nearer meal; late night is supper
    if it's a real meal, otherwise a snack."""
    t = local_dt.time()
    if time(4) <= t < time(10, 45):
        return "breakfast"
    if time(10, 45) <= t < time(16):
        return "lunch"
    if time(16) <= t < time(17):
        return "snack"
    if time(17) <= t < time(22):
        return "dinner"
    return "supper" if kcal >= SUPPER_MIN_KCAL else "snack"


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- users -------------------------------------------------------------

    def get_user(self, user_id: int) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return _user(row) if row else None

    def upsert_user(self, user_id: int, chat_id: int, first_name: str | None) -> User:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT INTO users (user_id, chat_id, first_name, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET chat_id = excluded.chat_id, first_name = excluded.first_name""",
            (user_id, chat_id, first_name, now),
        )
        self.conn.commit()
        return self.get_user(user_id)  # type: ignore[return-value]

    def set_user_field(self, user_id: int, field: str, value) -> None:
        if field not in {"authorized", "goal_kcal", "tz", "reminder_time", "notes", "code_attempts"}:
            raise ValueError(field)
        self.conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
        self.conn.commit()

    def last_activity(self, user_id: int) -> datetime:
        """When they last logged a meal (deleted ones count), or joined if they never have."""
        row = self.conn.execute(
            """SELECT MAX(t) t FROM (SELECT created_at t FROM users WHERE user_id = ?
                                     UNION ALL SELECT MAX(created_at) FROM meals WHERE user_id = ?)""",
            (user_id, user_id),
        ).fetchone()
        return datetime.fromisoformat(row["t"]).astimezone(UTC)

    def users_with_reminders(self) -> list[User]:
        rows = self.conn.execute(
            "SELECT * FROM users WHERE authorized = 1 AND reminder_time IS NOT NULL"
        ).fetchall()
        return [_user(r) for r in rows]

    # --- meals -------------------------------------------------------------

    def add_meal(
        self,
        user: User,
        *,
        eaten_at: datetime,
        title: str,
        kcal: float,
        protein_g: float,
        carbs_g: float,
        fat_g: float,
        estimate_json: str,
        source: str,
        photo_file_ids: list[str] | None = None,
        user_text: str | None = None,
        meal_type: str | None = None,
        dedupe_key: str | None = None,
    ) -> int:
        local = eaten_at.astimezone(user.zone)
        cur = self.conn.execute(
            """INSERT INTO meals (user_id, eaten_at, local_date, meal_type, title, kcal, protein_g, carbs_g,
                                  fat_g, estimate_json, source, photo_file_ids, user_text, dedupe_key, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user.user_id,
                eaten_at.astimezone(UTC).isoformat(timespec="seconds"),
                log_date(local).isoformat(),
                meal_type or guess_meal_type(local, kcal),
                title,
                round(kcal),
                round(protein_g, 1),
                round(carbs_g, 1),
                round(fat_g, 1),
                estimate_json,
                source,
                json.dumps(photo_file_ids or []),
                user_text,
                dedupe_key,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_meal(self, meal_id: int, **fields) -> None:
        allowed = {
            "title", "kcal", "protein_g", "carbs_g", "fat_g", "estimate_json", "portion",
            "meal_type", "bot_message_id", "deleted", "local_date", "eaten_at",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(bad)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE meals SET {cols} WHERE id = ?", (*fields.values(), meal_id))
        self.conn.commit()

    def get_meal(self, meal_id: int, user_id: int) -> Meal | None:
        row = self.conn.execute(
            "SELECT * FROM meals WHERE id = ? AND user_id = ? AND deleted = 0", (meal_id, user_id)
        ).fetchone()
        return _meal(row) if row else None

    def link_message(self, user_id: int, message_id: int, meal_id: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meal_links (user_id, message_id, meal_id) VALUES (?, ?, ?)",
            (user_id, message_id, meal_id),
        )
        self.conn.commit()

    def meal_by_bot_message(self, user_id: int, bot_message_id: int, include_deleted: bool = False) -> Meal | None:
        row = self.conn.execute(
            """SELECT m.* FROM meals m LEFT JOIN meal_links l ON l.meal_id = m.id AND l.user_id = m.user_id
               WHERE m.user_id = ? AND (m.deleted = 0 OR ?) AND (m.bot_message_id = ? OR l.message_id = ?)
               LIMIT 1""",
            (user_id, int(include_deleted), bot_message_id, bot_message_id),
        ).fetchone()
        return _meal(row) if row else None

    def recent_duplicate(self, user_id: int, dedupe_key: str, within: timedelta) -> Meal | None:
        since = (datetime.now(UTC) - within).isoformat(timespec="seconds")
        row = self.conn.execute(
            """SELECT * FROM meals WHERE user_id = ? AND dedupe_key = ? AND deleted = 0 AND created_at >= ?
               ORDER BY id DESC LIMIT 1""",
            (user_id, dedupe_key, since),
        ).fetchone()
        return _meal(row) if row else None

    def last_meal(self, user_id: int) -> Meal | None:
        row = self.conn.execute(
            "SELECT * FROM meals WHERE user_id = ? AND deleted = 0 ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        return _meal(row) if row else None

    def meals_on(self, user_id: int, day: date) -> list[Meal]:
        rows = self.conn.execute(
            "SELECT * FROM meals WHERE user_id = ? AND local_date = ? AND deleted = 0 ORDER BY eaten_at",
            (user_id, day.isoformat()),
        ).fetchall()
        return [_meal(r) for r in rows]

    def daily_totals(self, user_id: int, start: date, end: date) -> dict[date, dict[str, float]]:
        """Totals per local date in [start, end], including zero days."""
        rows = self.conn.execute(
            """SELECT local_date, SUM(kcal) kcal, SUM(protein_g) p, SUM(carbs_g) c, SUM(fat_g) f, COUNT(*) n
               FROM meals WHERE user_id = ? AND deleted = 0 AND local_date BETWEEN ? AND ?
               GROUP BY local_date""",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        by_day = {
            date.fromisoformat(r["local_date"]): {"kcal": r["kcal"], "protein_g": r["p"], "carbs_g": r["c"], "fat_g": r["f"], "n": r["n"]}
            for r in rows
        }
        out = {}
        d = start
        while d <= end:
            out[d] = by_day.get(d, {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "n": 0})
            d += timedelta(days=1)
        return out

    def all_meals(self, user_id: int) -> list[Meal]:
        rows = self.conn.execute(
            "SELECT * FROM meals WHERE user_id = ? AND deleted = 0 ORDER BY eaten_at", (user_id,)
        ).fetchall()
        return [_meal(r) for r in rows]

    # --- usage cap -----------------------------------------------------------

    def bump_usage(self, user_id: int, day: date) -> int:
        self.conn.execute(
            """INSERT INTO api_usage (user_id, local_date, calls) VALUES (?, ?, 1)
               ON CONFLICT(user_id, local_date) DO UPDATE SET calls = calls + 1""",
            (user_id, day.isoformat()),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT calls FROM api_usage WHERE user_id = ? AND local_date = ?", (user_id, day.isoformat())
        ).fetchone()
        return int(row["calls"])

    def usage(self, user_id: int, day: date) -> int:
        row = self.conn.execute(
            "SELECT calls FROM api_usage WHERE user_id = ? AND local_date = ?", (user_id, day.isoformat())
        ).fetchone()
        return int(row["calls"]) if row else 0

    def total_usage(self, day: date) -> int:
        row = self.conn.execute("SELECT COALESCE(SUM(calls), 0) n FROM api_usage WHERE local_date = ?", (day.isoformat(),))
        return int(row.fetchone()["n"])

    # --- backups ---------------------------------------------------------------

    def backup(self, directory: Path, keep: int = 14) -> Path:
        """Consistent online copy via SQLite's backup API; keeps the newest `keep` files."""
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"calbot-{datetime.now(UTC):%Y-%m-%d}.db"
        dest = sqlite3.connect(target)
        with dest:
            self.conn.backup(dest)
        dest.close()
        for old in sorted(directory.glob("calbot-*.db"))[:-keep]:
            old.unlink()
        return target


def _user(row: sqlite3.Row) -> User:
    return User(
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        first_name=row["first_name"],
        authorized=bool(row["authorized"]),
        goal_kcal=row["goal_kcal"],
        tz=row["tz"],
        reminder_time=row["reminder_time"],
        notes=row["notes"],
        code_attempts=row["code_attempts"],
    )


def _meal(row: sqlite3.Row) -> Meal:
    return Meal(
        id=row["id"],
        user_id=row["user_id"],
        eaten_at=datetime.fromisoformat(row["eaten_at"]).astimezone(UTC),
        local_date=date.fromisoformat(row["local_date"]),
        meal_type=row["meal_type"],
        title=row["title"],
        kcal=row["kcal"],
        protein_g=row["protein_g"],
        carbs_g=row["carbs_g"],
        fat_g=row["fat_g"],
        estimate_json=row["estimate_json"],
        deleted=bool(row["deleted"]),
        portion=row["portion"],
        source=row["source"],
        photo_file_ids=json.loads(row["photo_file_ids"] or "[]"),
        user_text=row["user_text"],
        bot_message_id=row["bot_message_id"],
    )
