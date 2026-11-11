#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот для сбора средств в Telegram (v2).

Что нового по сравнению со старой версией
-----------------------------------------
* Админ-панель на кнопках (/admin): «Сборы» -> нужный сбор -> фамилии ->
  отметить одного или нескольких человек. Кнопки видят и нажимают только
  администраторы из ADMIN_IDS.
* Кнопка «Сдали все сборы»: выбираем фамилию человека и отмечаем её сразу
  во всех сборах этого чата.
* Живой список должников: сообщение не отправляется заново, а редактируется
  на месте при каждой отметке (кнопками или текстом «Иванов+»).
* Нумерация сборов всегда 1..N: после удаления сбора «дыры» в номерах
  исчезают, удалённый сбор больше не занимает номер.
* Старая база collections.db подхватывается как есть. Перед миграцией
  автоматически создаётся резервная копия в папке backups/.
* Все старые команды и текстовые отметки («Иванов+», «список должников»)
  продолжают работать.

Запуск:  python bot.py
Переменные окружения:
  BOT_TOKEN            — токен бота (обязательно);
  ADMIN_IDS            — id администраторов через запятую;
  DB_PATH              — путь к файлу базы (по умолчанию collections.db);
  AUTO_DELETE_SECONDS  — через сколько удалять короткие подтверждения (0 — не удалять).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ======================================================================
# НАСТРОЙКИ
# ======================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()


def _parse_admin_ids() -> list[int]:
    raw = (os.getenv("ADMIN_IDS") or "").strip()
    parts = [p for p in re.split(r"[,;\s]+", raw) if p]
    ids = []
    for p in parts:
        try:
            ids.append(int(p))
        except ValueError:
            logging.warning("ADMIN_IDS: пропускаю некорректное значение %r", p)
    return ids


ADMIN_IDS = _parse_admin_ids() or [1920218354]   # ваш user_id

# Автоудаление коротких подтверждений (в секундах). 0 — выключить.
try:
    AUTO_DELETE_SECONDS = max(0, int(os.getenv("AUTO_DELETE_SECONDS") or 15))
except ValueError:
    AUTO_DELETE_SECONDS = 15

SCHEMA_VERSION = 2


def _db_richness(path: str) -> tuple[int, int, float]:
    """Сколько данных в файле базы — чтобы выбрать «настоящую» из двух копий."""
    if not os.path.exists(path):
        return (0, 0, 0.0)
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            collections = conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
            members = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        finally:
            conn.close()
        return (collections, members, os.path.getmtime(path))
    except (sqlite3.Error, OSError):
        return (0, 0, 0.0)


def _detect_data_dir() -> Optional[str]:
    """Постоянная папка BotHost (/app/data). Локально — None.

    На BotHost каталог /app пересоздаётся при обновлении из Git, а /app/data
    сохраняется. Поэтому база должна жить именно там.
    """
    explicit = (os.getenv("DATA_DIR") or "").strip()
    if explicit:
        try:
            os.makedirs(explicit, exist_ok=True)
        except OSError:
            logging.exception("Не удалось создать DATA_DIR %s", explicit)
        return explicit

    if os.name != "nt" and os.path.isdir("/app"):
        try:
            os.makedirs("/app/data", exist_ok=True)
            if os.access("/app/data", os.W_OK):
                return "/app/data"
        except OSError:
            logging.exception("Не удалось подготовить /app/data")
    return None


DATA_DIR = _detect_data_dir()


def _resolve_db_path() -> str:
    """Путь к базе: на BotHost — /app/data, локально — рядом со скриптом."""
    explicit = (os.getenv("DB_PATH") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    if DATA_DIR:
        return os.path.join(DATA_DIR, "collections.db")

    raw = "collections.db"
    near_script = os.path.join(BASE_DIR, raw)
    in_cwd = os.path.abspath(raw)

    has_near = os.path.exists(near_script)
    has_cwd = in_cwd != near_script and os.path.exists(in_cwd)

    if has_near and has_cwd:
        # две копии — берём ту, где больше данных (при равенстве — что свежее)
        near_score = _db_richness(near_script)
        cwd_score = _db_richness(in_cwd)
        if cwd_score > near_score:
            logging.warning(
                "Найдены две базы: использую %s (%s сборов), а не %s",
                in_cwd, cwd_score[0], near_script,
            )
            return in_cwd
        logging.warning(
            "Найдены две базы: использую %s (%s сборов), а не %s",
            near_script, near_score[0], in_cwd,
        )
        return near_script

    if has_near:
        return near_script
    if has_cwd:
        logging.warning("База найдена в рабочем каталоге: %s", in_cwd)
        return in_cwd
    return near_script


DB_PATH = _resolve_db_path()
BACKUP_DIR = os.getenv("BACKUP_DIR") or os.path.join(
    os.path.dirname(DB_PATH) or ".", "backups"
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ======================================================================
# БАЗА ДАННЫХ
# ======================================================================

DDL = """
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    active INTEGER DEFAULT 1,
    amount INTEGER DEFAULT 0,
    chat_id INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL,
    family TEXT NOT NULL,
    paid INTEGER DEFAULT 0,
    paid_at TEXT
);
CREATE TABLE IF NOT EXISTS families (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER DEFAULT 0,
    name TEXT NOT NULL,
    UNIQUE(chat_id, name)
);
CREATE TABLE IF NOT EXISTS ui_messages (
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    part INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, kind, part)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db():
    conn = db_connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _read_schema_version() -> int:
    if not os.path.exists(DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0
    except (sqlite3.Error, TypeError, ValueError):
        return 0


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _migrate(conn: sqlite3.Connection) -> None:
    """Аккуратно докладываем недостающие колонки/таблицы, ничего не удаляя."""
    col_cols = _table_columns(conn, "collections")
    if "amount" not in col_cols:
        conn.execute("ALTER TABLE collections ADD COLUMN amount INTEGER DEFAULT 0")
    if "chat_id" not in col_cols:
        conn.execute("ALTER TABLE collections ADD COLUMN chat_id INTEGER DEFAULT 0")

    mem_cols = _table_columns(conn, "members")
    if "paid" not in mem_cols:
        conn.execute("ALTER TABLE members ADD COLUMN paid INTEGER DEFAULT 0")
    if "paid_at" not in mem_cols:
        conn.execute("ALTER TABLE members ADD COLUMN paid_at TEXT")

    fam_cols = _table_columns(conn, "families")
    if "chat_id" not in fam_cols:
        # совсем старая схема: families(id, name) без привязки к чату
        conn.execute("DROP TABLE IF EXISTS families_old")
        conn.execute("ALTER TABLE families RENAME TO families_old")
        conn.execute(
            """CREATE TABLE families (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   chat_id INTEGER DEFAULT 0,
                   name TEXT NOT NULL,
                   UNIQUE(chat_id, name)
               )"""
        )
        conn.execute(
            "INSERT OR IGNORE INTO families (chat_id, name) "
            "SELECT 0, name FROM families_old"
        )
        conn.execute("DROP TABLE families_old")

    # дубликаты фамилий не нужны, но сами фамилии сохраняем
    conn.execute(
        "DELETE FROM families WHERE id NOT IN "
        "(SELECT MIN(id) FROM families GROUP BY chat_id, name)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_families_chat_name "
        "ON families(chat_id, name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_members_collection "
        "ON members(collection_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collections_chat "
        "ON collections(chat_id)"
    )


def _legacy_db_candidates() -> list[str]:
    """Где старый бот мог оставить базу до перехода на /app/data."""
    candidates = [
        os.path.join("/app", "collections.db"),
        os.path.join(BASE_DIR, "collections.db"),
        os.path.abspath("collections.db"),
    ]
    result, seen = [], set()
    for path in candidates:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _import_legacy_db_file(candidates=None) -> Optional[str]:
    """Первый запуск в контейнере: переносим старую базу в постоянную папку.

    Копируем (не перемещаем), чтобы даже при сбое старый файл остался на месте.
    """
    if not DATA_DIR or os.path.exists(DB_PATH):
        return None
    for candidate in (candidates if candidates is not None else _legacy_db_candidates()):
        try:
            if candidate == DB_PATH:
                continue
            if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
                shutil.copy2(candidate, DB_PATH)
                log.warning(
                    "Старая база скопирована в постоянное хранилище: %s -> %s",
                    candidate, DB_PATH,
                )
                return candidate
        except OSError:
            log.exception("Не удалось перенести старую базу из %s", candidate)
    return None


def init_db() -> None:
    """Создаёт/обновляет схему. Перед изменениями делает бэкап."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    _import_legacy_db_file()

    existed = os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0
    version = _read_schema_version() if existed else 0

    if existed and version < SCHEMA_VERSION:
        try:
            path = backup_db("before-migrate")
            if path:
                log.info("Резервная копия базы перед обновлением: %s", path)
        except Exception:
            log.exception("Не удалось создать резервную копию базы")

    with db() as conn:
        conn.executescript(DDL)
        _migrate(conn)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )


def backup_db(tag: str = "manual") -> Optional[str]:
    """Копия базы через sqlite backup API (безопасно при открытом файле)."""
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"collections-{stamp}-{tag}.db")
    src = sqlite3.connect(DB_PATH, timeout=30)
    try:
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    _prune_backups()
    return dst


def _prune_backups(keep: int = 10) -> None:
    try:
        files = [
            os.path.join(BACKUP_DIR, name)
            for name in os.listdir(BACKUP_DIR)
            if name.endswith(".db")
        ]
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            os.remove(old)
    except OSError:
        log.exception("Не удалось почистить старые резервные копии")


# ======================================================================
# СПРАВОЧНИКИ / МОДЕЛЬ
# ======================================================================

def norm_name(value: str) -> str:
    """Нормализация фамилии для сравнения: регистр, лишние пробелы, ё/е."""
    text = re.sub(r"\s+", " ", (value or "").strip())
    return text.casefold().replace("ё", "е")


def fetch_collections(chat_id: int) -> list[dict]:
    """Все сборы чата по порядку. `num` — отображаемый номер 1..N без дыр."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.created_at, c.active, c.amount,
                   (SELECT COUNT(*) FROM members m
                     WHERE m.collection_id = c.id) AS total,
                   (SELECT COUNT(*) FROM members m
                     WHERE m.collection_id = c.id AND m.paid = 1) AS paid
            FROM collections c
            WHERE c.chat_id = ?
            ORDER BY c.id
            """,
            (chat_id,),
        ).fetchall()

    result = []
    for num, row in enumerate(rows, 1):
        result.append(
            {
                "num": num,
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "active": bool(row["active"]),
                "amount": row["amount"] or 0,
                "total": row["total"] or 0,
                "paid": row["paid"] or 0,
            }
        )
    return result


def get_collection(cid: int, chat_id: Optional[int] = None) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT id, title, created_at, active, amount, chat_id "
            "FROM collections WHERE id = ?",
            (cid,),
        ).fetchone()
        if not row:
            return None
        if chat_id is not None and row["chat_id"] != chat_id:
            return None
        owner_chat = row["chat_id"]
        num = conn.execute(
            "SELECT COUNT(*) FROM collections WHERE chat_id = ? AND id <= ?",
            (owner_chat, cid),
        ).fetchone()[0]
    return {
        "num": num,
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "active": bool(row["active"]),
        "amount": row["amount"] or 0,
    }


def collection_by_number(chat_id: int, num: int) -> Optional[dict]:
    """Сбор по отображаемому номеру (#1, #2, ...)."""
    if num < 1:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM collections WHERE chat_id = ? "
            "ORDER BY id LIMIT 1 OFFSET ?",
            (chat_id, num - 1),
        ).fetchone()
    return get_collection(row["id"], chat_id) if row else None


def get_active_collection(chat_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM collections WHERE active = 1 AND chat_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    return get_collection(row["id"], chat_id) if row else None


def get_members(cid: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, family, paid, paid_at FROM members "
            "WHERE collection_id = ? ORDER BY family COLLATE NOCASE, id",
            (cid,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "family": r["family"],
            "paid": bool(r["paid"]),
            "paid_at": r["paid_at"],
        }
        for r in rows
    ]


def create_collection(chat_id: int, title: str, families, amount: int = 0) -> int:
    with db() as conn:
        conn.execute(
            "UPDATE collections SET active = 0 WHERE active = 1 AND chat_id = ?",
            (chat_id,),
        )
        cur = conn.execute(
            "INSERT INTO collections (title, active, amount, chat_id) "
            "VALUES (?, 1, ?, ?)",
            (title, amount, chat_id),
        )
        cid = cur.lastrowid
        for family in families:
            conn.execute(
                "INSERT INTO members (collection_id, family) VALUES (?, ?)",
                (cid, family),
            )
    return cid


def set_member_paid(mid: int, paid: bool) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE members SET paid = ?, "
            "paid_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE NULL END "
            "WHERE id = ?",
            (int(paid), int(paid), mid),
        )


def toggle_member(mid: int, chat_id: int) -> Optional[dict]:
    """Переключает отметку. Возвращает данные участника или None."""
    with db() as conn:
        row = conn.execute(
            "SELECT m.id, m.collection_id, m.family, m.paid "
            "FROM members m JOIN collections c ON c.id = m.collection_id "
            "WHERE m.id = ? AND c.chat_id = ?",
            (mid, chat_id),
        ).fetchone()
        if not row:
            return None
        new_paid = 0 if row["paid"] else 1
        conn.execute(
            "UPDATE members SET paid = ?, "
            "paid_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE NULL END "
            "WHERE id = ?",
            (new_paid, new_paid, mid),
        )
    return {
        "id": mid,
        "collection_id": row["collection_id"],
        "family": row["family"],
        "paid": bool(new_paid),
    }


def set_collection_paid(cid: int, chat_id: int, paid: bool) -> int:
    """Отметить/снять всех в сборе. Возвращает число изменённых записей."""
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM collections WHERE id = ? AND chat_id = ?",
            (cid, chat_id),
        ).fetchone()
        if not row:
            return 0
        cur = conn.execute(
            "UPDATE members SET paid = ?, "
            "paid_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE NULL END "
            "WHERE collection_id = ? AND paid <> ?",
            (int(paid), int(paid), cid, int(paid)),
        )
        return cur.rowcount or 0


def _match_member(members: list[dict], family: str) -> Optional[dict]:
    target = norm_name(family)
    if not target:
        return None
    exact = [m for m in members if norm_name(m["family"]) == target]
    if exact:
        return exact[0]
    if len(target) >= 3:
        prefix = [m for m in members if norm_name(m["family"]).startswith(target)]
        if len(prefix) == 1:
            return prefix[0]
    return None


def mark_paid(cid: int, chat_id: int, family: str) -> tuple[str, Optional[str]]:
    col = get_collection(cid, chat_id)
    if not col:
        return "no_collection", None
    member = _match_member(get_members(cid), family)
    if not member:
        return "not_found", None
    if member["paid"]:
        return "already", member["family"]
    set_member_paid(member["id"], True)
    return "ok", member["family"]


def unmark_paid(cid: int, chat_id: int, family: str) -> tuple[str, Optional[str]]:
    col = get_collection(cid, chat_id)
    if not col:
        return "no_collection", None
    member = _match_member(get_members(cid), family)
    if not member:
        return "not_found", None
    if not member["paid"]:
        return "already_unpaid", member["family"]
    set_member_paid(member["id"], False)
    return "ok", member["family"]


def family_membership(chat_id: int, family: str) -> list[tuple[dict, dict]]:
    """Все пары (сбор, участник) для фамилии в этом чате."""
    target = norm_name(family)
    result = []
    for col in fetch_collections(chat_id):
        for member in get_members(col["id"]):
            if norm_name(member["family"]) == target:
                result.append((col, member))
    return result


def find_unpaid_in_past(chat_id: int, family: str) -> list[tuple[dict, dict]]:
    return [
        (col, member)
        for col, member in family_membership(chat_id, family)
        if not col["active"] and not member["paid"]
    ]


def mark_family_everywhere(chat_id: int, family: str):
    """Отмечает фамилию во всех сборах чата. -> (отмеченные сборы, уже были)."""
    marked, already = [], []
    for col, member in family_membership(chat_id, family):
        if member["paid"]:
            already.append(col)
        else:
            set_member_paid(member["id"], True)
            marked.append(col)
    return marked, already


def get_families(chat_id: int) -> list[str]:
    return [row["name"] for row in family_rows(chat_id)]


def family_rows(chat_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name FROM families WHERE chat_id = ? "
            "ORDER BY name COLLATE NOCASE",
            (chat_id,),
        ).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def get_family(chat_id: int, fid: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT id, name FROM families WHERE id = ? AND chat_id = ?",
            (fid, chat_id),
        ).fetchone()
    return {"id": row["id"], "name": row["name"]} if row else None


def set_families(chat_id: int, names) -> None:
    with db() as conn:
        conn.execute("DELETE FROM families WHERE chat_id = ?", (chat_id,))
        for name in names:
            conn.execute(
                "INSERT OR IGNORE INTO families (chat_id, name) VALUES (?, ?)",
                (chat_id, name),
            )


def delete_collection(cid: int, chat_id: int):
    col = get_collection(cid, chat_id)
    if not col:
        return None
    with db() as conn:
        members_count = conn.execute(
            "SELECT COUNT(*) FROM members WHERE collection_id = ?", (cid,)
        ).fetchone()[0]
        conn.execute("DELETE FROM members WHERE collection_id = ?", (cid,))
        conn.execute("DELETE FROM collections WHERE id = ?", (cid,))
    return col["title"], col["num"], members_count


def delete_all_collections(chat_id: int) -> int:
    with db() as conn:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM collections WHERE chat_id = ?", (chat_id,)
            ).fetchall()
        ]
        if not ids:
            return 0
        qmarks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM members WHERE collection_id IN ({qmarks})", ids)
        cur = conn.execute("DELETE FROM collections WHERE chat_id = ?", (chat_id,))
        return cur.rowcount or 0


def bind_old_data_to_chat(chat_id: int) -> tuple[int, int]:
    with db() as conn:
        cur = conn.execute(
            "UPDATE collections SET chat_id = ? WHERE chat_id = 0", (chat_id,)
        )
        col_n = cur.rowcount or 0
        cur = conn.execute(
            "UPDATE families SET chat_id = ? WHERE chat_id = 0", (chat_id,)
        )
        fam_n = cur.rowcount or 0
    return col_n, fam_n


def transfer_data(from_chat_id: int, to_chat_id: int) -> tuple[int, int]:
    """Переносит сборы и фамилии из одного чата в другой (с удалением в источнике)."""
    with db() as conn:
        cols = conn.execute(
            "SELECT id, title, created_at, active, amount FROM collections "
            "WHERE chat_id = ?",
            (from_chat_id,),
        ).fetchall()

        col_count = 0
        for row in cols:
            cur = conn.execute(
                "INSERT INTO collections (title, created_at, active, amount, chat_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (row["title"], row["created_at"], row["active"], row["amount"],
                 to_chat_id),
            )
            new_cid = cur.lastrowid
            conn.execute(
                "INSERT INTO members (collection_id, family, paid, paid_at) "
                "SELECT ?, family, paid, paid_at FROM members WHERE collection_id = ?",
                (new_cid, row["id"]),
            )
            col_count += 1

        old_ids = [row["id"] for row in cols]
        if old_ids:
            qmarks = ",".join("?" * len(old_ids))
            conn.execute(
                f"DELETE FROM members WHERE collection_id IN ({qmarks})", old_ids
            )
            conn.execute(
                "DELETE FROM collections WHERE chat_id = ?", (from_chat_id,)
            )

        fams = conn.execute(
            "SELECT name FROM families WHERE chat_id = ?", (from_chat_id,)
        ).fetchall()
        fam_count = 0
        for fam in fams:
            exists = conn.execute(
                "SELECT 1 FROM families WHERE chat_id = ? AND LOWER(name) = LOWER(?)",
                (to_chat_id, fam["name"]),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO families (chat_id, name) VALUES (?, ?)",
                    (to_chat_id, fam["name"]),
                )
                fam_count += 1
        conn.execute("DELETE FROM families WHERE chat_id = ?", (from_chat_id,))
    return col_count, fam_count


# ---------- «Живые» сообщения (редактируются на месте) ----------

KIND_DEBTORS = "debtors"


def live_parts(chat_id: int, kind: str) -> list[tuple[int, int]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT part, message_id FROM ui_messages "
            "WHERE chat_id = ? AND kind = ? ORDER BY part",
            (chat_id, kind),
        ).fetchall()
    return [(r["part"], r["message_id"]) for r in rows]


def save_live_part(chat_id: int, kind: str, part: int, message_id: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO ui_messages (chat_id, kind, part, message_id, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(chat_id, kind, part) DO UPDATE SET "
            "message_id = excluded.message_id, updated_at = CURRENT_TIMESTAMP",
            (chat_id, kind, part, message_id),
        )


def drop_live_part(chat_id: int, kind: str, part: int) -> None:
    with db() as conn:
        conn.execute(
            "DELETE FROM ui_messages WHERE chat_id = ? AND kind = ? AND part = ?",
            (chat_id, kind, part),
        )


def clear_live(chat_id: int, kind: str) -> None:
    with db() as conn:
        conn.execute(
            "DELETE FROM ui_messages WHERE chat_id = ? AND kind = ?",
            (chat_id, kind),
        )


# ======================================================================
# ТЕКСТЫ
# ======================================================================

def md_escape(value) -> str:
    """Экранирует символы Markdown, чтобы фамилии не ломали разметку."""
    text = "" if value is None else str(value)
    return re.sub(r"([_*`\[\]])", r"\\\1", text)


def shorten(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def split_long(text: str, limit: int = 3800) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur:
                parts.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        parts.append(cur)
    return parts


def money(value: int) -> str:
    return f"{value} ₽" if value else "—"


def render_debtors(chat_id: int) -> str:
    """Общий список должников по всем сборам чата (как в старой версии)."""
    totals: dict[str, dict] = {}
    blocks = []

    for col in fetch_collections(chat_id):
        members = get_members(col["id"])
        unpaid = [m for m in members if not m["paid"]]
        if not unpaid:
            continue
        amount = col["amount"]
        amount_str = f"по {amount} ₽" if amount else "сумма не указана"
        lines = [
            f"📌 *#{col['num']} «{md_escape(col['title'])}»* _{amount_str}_ — "
            f"сдало *{col['paid']}/{col['total']}*"
        ]
        for member in unpaid:
            lines.append(f"   • {md_escape(member['family'])} — {money(amount)}")
            key = norm_name(member["family"])
            agg = totals.setdefault(
                key, {"name": member["family"], "sum": 0, "count": 0}
            )
            agg["sum"] += amount
            agg["count"] += 1
        blocks.append("\n".join(lines))

    if not blocks:
        return "🎉 Должников нет — все сдали!"

    text = "💸 *Список должников*\n\n" + "\n\n".join(blocks)
    text += "\n\n━━━━━━━━━━━━━━\n📊 *Итого по людям (по всем сборам этого чата):*"
    for agg in sorted(totals.values(), key=lambda x: (-x["sum"], x["name"])):
        text += f"\n• {md_escape(agg['name'])} — *{money(agg['sum'])}*"
    return text


def render_collection_debtors(col: dict) -> str:
    """Должники по одному сбору (старый формат `/debtors N`)."""
    members = get_members(col["id"])
    unpaid = [m["family"] for m in members if not m["paid"]]
    paid = [m["family"] for m in members if m["paid"]]
    amount = col["amount"]

    header = f"💸 *Должники по сбору #{col['num']} «{md_escape(col['title'])}»*"
    if amount:
        header += f"\n_Сумма: {amount} ₽ с человека_"
    header += f"\n_Сдало: *{len(paid)}/{len(members)}*_\n"

    if not unpaid:
        return header + f"\n🎉 Все сдали! ({len(paid)}/{len(members)})"

    lines = [header]
    total_debt = 0
    for i, family in enumerate(unpaid, 1):
        lines.append(f"{i}. {md_escape(family)} — {money(amount)}")
        total_debt += amount
    if amount:
        lines.append(f"\n💰 *Итого к сбору: {total_debt} ₽*")
    lines.append(f"\n✅ Сдало: {len(paid)}/{len(members)}")
    if paid:
        lines.append(f"Список сдавших: {md_escape(', '.join(paid))}")
    return "\n".join(lines)


def render_all(chat_id: int) -> str:
    cols = fetch_collections(chat_id)
    if not cols:
        return "Сборов в этом чате пока нет."

    blocks = []
    for col in cols:
        members = get_members(col["id"])
        paid = [m["family"] for m in members if m["paid"]]
        unpaid = [m["family"] for m in members if not m["paid"]]
        status = "🟢 активный" if col["active"] else "⚪ завершён"
        amount = f" • {col['amount']} ₽ с чел." if col["amount"] else ""
        block = f"*#{col['num']} — {md_escape(col['title'])}*\n"
        block += f"_Создан: {col['created_at']} • {status}{amount}_\n"
        block += (
            f"✅ Сдало ({len(paid)}/{len(members)}): "
            f"{md_escape(', '.join(paid)) or '—'}\n"
        )
        if unpaid:
            block += (
                f"❌ Должники ({len(unpaid)}): {md_escape(', '.join(unpaid))}"
            )
        blocks.append(block)
    return "\n\n".join(blocks)


MEMBERS_PER_PAGE = 20
COLLECTIONS_PER_PAGE = 8
FAMILIES_PER_PAGE = 20


def render_collection_page(chat_id: int, cid: int, page: int = 0):
    """Страница сбора для админ-панели. -> (текст, страница, всего, участники)."""
    col = get_collection(cid, chat_id)
    if not col:
        return None, 0, 1, []
    members = get_members(cid)
    total_pages = max(1, (len(members) + MEMBERS_PER_PAGE - 1) // MEMBERS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = members[page * MEMBERS_PER_PAGE:(page + 1) * MEMBERS_PER_PAGE]

    amount = col["amount"]
    paid_cnt = sum(1 for m in members if m["paid"])
    total = len(members)

    lines = [f"📌 *#{col['num']} «{md_escape(col['title'])}»*"]
    meta = ["🟢 активный" if col["active"] else "⚪ завершён"]
    if amount:
        meta.insert(0, f"по {amount} ₽ с человека")
    lines.append("_" + " • ".join(meta) + "_")
    line = f"Сдало *{paid_cnt}/{total}*"
    if amount:
        line += f" • осталось собрать *{(total - paid_cnt) * amount} ₽*"
    lines.append(line)
    if total_pages > 1:
        lines.append(f"_Страница {page + 1}/{total_pages}_")
    lines.append("")
    for member in chunk:
        lines.append(("✅ " if member["paid"] else "⬜ ") + md_escape(member["family"]))
    lines.append("")
    lines.append("_Нажмите на фамилию, чтобы отметить оплату._")
    return "\n".join(lines), page, total_pages, chunk


# ======================================================================
# КЛАВИАТУРЫ
# ======================================================================

ADMIN_PANEL_TEXT = (
    "⚙️ *Панель администратора*\n\n"
    "• *Сборы* — выбрать сбор и отметить, кто сдал\n"
    "• *Должники* — живой список, обновляется на месте\n"
    "• *Сдали все сборы* — отметить человека сразу во всех сборах\n\n"
    "_Кнопки видит только администратор. Панель показывает сборы того чата,_\n"
    "_в котором вы её открыли._"
)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def main_menu_kb() -> InlineKeyboardMarkup:
    return kb(
        [
            [InlineKeyboardButton("📋 Сборы", callback_data="cl:0")],
            [InlineKeyboardButton("💸 Живой список должников", callback_data="dv")],
            [InlineKeyboardButton("✅ Сдали все сборы", callback_data="af:0")],
            [
                InlineKeyboardButton("🆕 Новый сбор", callback_data="nw"),
                InlineKeyboardButton("👥 Фамилии", callback_data="fl"),
            ],
            [InlineKeyboardButton("✖️ Закрыть", callback_data="x")],
        ]
    )


def collections_kb(chat_id: int, page: int = 0) -> InlineKeyboardMarkup:
    cols = fetch_collections(chat_id)
    total_pages = max(1, (len(cols) + COLLECTIONS_PER_PAGE - 1) // COLLECTIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = cols[page * COLLECTIONS_PER_PAGE:(page + 1) * COLLECTIONS_PER_PAGE]

    rows = []
    for col in chunk:
        icon = "🟢" if col["active"] else "⚪"
        label = (
            f"{icon} {col['num']}. {shorten(col['title'], 20)} — "
            f"{col['paid']}/{col['total']}"
        )
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"c:{col['id']}:0")]
        )
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"cl:{page - 1}"))
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"cl:{page}")
        )
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"cl:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("⚙️ Меню", callback_data="m")])
    return kb(rows)


def collection_kb(
    chat_id: int, cid: int, page: int, total_pages: int, chunk: list[dict]
) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(chunk), 2):
        row = []
        for member in chunk[i:i + 2]:
            label = ("✅ " if member["paid"] else "⬜ ") + shorten(member["family"], 18)
            row.append(
                InlineKeyboardButton(
                    label, callback_data=f"t:{cid}:{page}:{member['id']}"
                )
            )
        rows.append(row)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"c:{cid}:{page - 1}"))
        nav.append(
            InlineKeyboardButton(
                f"{page + 1}/{total_pages}", callback_data=f"c:{cid}:{page}"
            )
        )
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"c:{cid}:{page + 1}"))
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton("✅ Все сдали", callback_data=f"pa:{cid}:{page}"),
            InlineKeyboardButton("↩️ Снять всех", callback_data=f"pu:{cid}:{page}"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("🔄 Обновить", callback_data=f"c:{cid}:{page}"),
            InlineKeyboardButton("🗑 Удалить сбор", callback_data=f"dc:{cid}"),
        ]
    )
    rows.append([InlineKeyboardButton("⬅️ К списку сборов", callback_data="cl:0")])
    rows.append([InlineKeyboardButton("⚙️ Меню", callback_data="m")])
    return kb(rows)


def families_kb(chat_id: int, page: int = 0) -> InlineKeyboardMarkup:
    fams = family_rows(chat_id)
    total_pages = max(1, (len(fams) + FAMILIES_PER_PAGE - 1) // FAMILIES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = fams[page * FAMILIES_PER_PAGE:(page + 1) * FAMILIES_PER_PAGE]

    rows = []
    for i in range(0, len(chunk), 2):
        row = []
        for fam in chunk[i:i + 2]:
            row.append(
                InlineKeyboardButton(
                    shorten(fam["name"], 20), callback_data=f"as:{fam['id']}"
                )
            )
        rows.append(row)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"af:{page - 1}"))
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"af:{page}")
        )
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"af:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("⚙️ Меню", callback_data="m")])
    return kb(rows)


# ======================================================================
# ВСПОМОГАТЕЛЬНОЕ ДЛЯ ОТПРАВКИ/РЕДАКТИРОВАНИЯ
# ======================================================================

async def safe_edit(bot, chat_id: int, message_id: int, text: str,
                    reply_markup=None) -> None:
    """Редактирует сообщение; при проблемах с Markdown пробует без разметки."""
    common = dict(chat_id=chat_id, message_id=message_id, text=text,
                  reply_markup=reply_markup)
    try:
        await bot.edit_message_text(parse_mode=ParseMode.MARKDOWN, **common)
        return
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        log.warning("Не удалось изменить сообщение с Markdown: %s", exc)
    try:
        await bot.edit_message_text(**common)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def update_live(bot, chat_id: int, kind: str, parts: list[str]) -> None:
    """Обновляет «живое» сообщение: правит существующие, не создаёт новые."""
    existing = dict(live_parts(chat_id, kind))

    for index, text in enumerate(parts):
        mid = existing.pop(index, None)
        if mid is not None:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=mid, text=text,
                    parse_mode=ParseMode.MARKDOWN,
                )
                continue
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    continue
                log.info("Живое сообщение %s/%s не изменилось: %s", kind, index, exc)
            except TelegramError as exc:
                log.info("Живое сообщение %s/%s: %s", kind, index, exc)

        try:
            msg = await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            msg = await bot.send_message(chat_id=chat_id, text=text)
        save_live_part(chat_id, kind, index, msg.message_id)

    # если частей стало меньше — убираем лишние
    for index, mid in existing.items():
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except TelegramError:
            pass
        drop_live_part(chat_id, kind, index)


async def refresh_debtors(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Обновить живой список должников, если он есть в этом чате."""
    if not live_parts(chat_id, KIND_DEBTORS):
        return
    parts = split_long(render_debtors(chat_id))
    await update_live(context.bot, chat_id, KIND_DEBTORS, parts)


async def send_debtors(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    parts = split_long(render_debtors(chat_id))
    await update_live(context.bot, chat_id, KIND_DEBTORS, parts)


# ======================================================================
# АДМИН-ПАНЕЛЬ (колбэки)
# ======================================================================

async def show_collections(bot, chat_id: int, message_id: int, page: int = 0) -> None:
    cols = fetch_collections(chat_id)
    if not cols:
        await safe_edit(
            bot, chat_id, message_id,
            "📋 В этом чате сборов пока нет.\n\n"
            "Создайте: `/new Подарок 300`\n\n"
            "_Панель показывает сборы того чата, в котором она открыта._",
            main_menu_kb(),
        )
        return

    total_pages = max(1, (len(cols) + COLLECTIONS_PER_PAGE - 1) // COLLECTIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = cols[page * COLLECTIONS_PER_PAGE:(page + 1) * COLLECTIONS_PER_PAGE]

    lines = ["📋 *Сборы*\n"]
    for col in chunk:
        icon = "🟢" if col["active"] else "⚪"
        amount = f" • {col['amount']} ₽" if col["amount"] else ""
        lines.append(
            f"{icon} *#{col['num']}* {md_escape(col['title'])} — "
            f"сдало *{col['paid']}/{col['total']}*{amount}"
        )
    lines.append("\n_Выберите сбор, чтобы отметить сдавших._")
    await safe_edit(
        bot, chat_id, message_id, "\n".join(lines), collections_kb(chat_id, page)
    )


async def show_collection_view(bot, chat_id: int, message_id: int, cid: int,
                               page: int = 0) -> None:
    text, page, total_pages, chunk = render_collection_page(chat_id, cid, page)
    if text is None:
        await safe_edit(
            bot, chat_id, message_id,
            "⚠️ Сбор не найден — возможно, он удалён.",
            main_menu_kb(),
        )
        return
    await safe_edit(
        bot, chat_id, message_id, text,
        collection_kb(chat_id, cid, page, total_pages, chunk),
    )


async def show_families_picker(bot, chat_id: int, message_id: int,
                               page: int = 0) -> None:
    fams = family_rows(chat_id)
    if not fams:
        await safe_edit(
            bot, chat_id, message_id,
            "👥 Список фамилий пуст.\n\nЗадайте его командой:\n"
            "`/setfamilies`\nИванов\nПетров\nСидоров",
            main_menu_kb(),
        )
        return
    total_pages = max(1, (len(fams) + FAMILIES_PER_PAGE - 1) // FAMILIES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    text = (
        "✅ *Сдали все сборы*\n\n"
        "Выберите человека — отмечу его во всех сборах этого чата."
    )
    if total_pages > 1:
        text += f"\n_Страница {page + 1}/{total_pages}_"
    await safe_edit(bot, chat_id, message_id, text, families_kb(chat_id, page))


def render_family_confirm(chat_id: int, family: dict) -> tuple[str, int]:
    pairs = family_membership(chat_id, family["name"])
    unpaid, already = [], []
    for col, member in pairs:
        (already if member["paid"] else unpaid).append(col)

    lines = [f"👤 *{md_escape(family['name'])}*"]
    if not pairs:
        lines.append("\nЭтот человек не участвует ни в одном сборе.")
    elif not unpaid:
        lines.append("\n✅ Уже отмечен во всех сборах.")
    else:
        lines.append(f"\nБудет отмечено в *{len(unpaid)}* сборах:")
        for col in unpaid:
            lines.append(f"• #{col['num']} «{md_escape(col['title'])}»")
    if already:
        lines.append(f"\n_Уже отмечено в {len(already)} сборах._")
    return "\n".join(lines), len(unpaid)


async def show_family_confirm(bot, chat_id: int, message_id: int, fid: int) -> None:
    family = get_family(chat_id, fid)
    if not family:
        await safe_edit(
            bot, chat_id, message_id, "⚠️ Фамилия не найдена.", families_kb(chat_id, 0)
        )
        return
    text, unpaid_count = render_family_confirm(chat_id, family)

    rows = []
    if unpaid_count:
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅ Отметить в {unpaid_count} сборах",
                    callback_data=f"ay:{family['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ К списку фамилий", callback_data="af:0")])
    rows.append([InlineKeyboardButton("⚙️ Меню", callback_data="m")])
    await safe_edit(bot, chat_id, message_id, text, kb(rows))


async def apply_family_everywhere(bot, chat_id: int, message_id: int,
                                  fid: int) -> None:
    family = get_family(chat_id, fid)
    if not family:
        await safe_edit(
            bot, chat_id, message_id, "⚠️ Фамилия не найдена.", families_kb(chat_id, 0)
        )
        return

    marked, already = mark_family_everywhere(chat_id, family["name"])
    lines = [f"✅ *{md_escape(family['name'])}* — оплата отмечена."]
    if marked:
        lines.append(f"\nОтмечено в *{len(marked)}* сборах:")
        for col in marked:
            lines.append(f"• #{col['num']} «{md_escape(col['title'])}»")
    if already:
        lines.append(f"\n_Уже было отмечено в {len(already)} сборах._")
    if not marked and not already:
        lines.append("\nЭтот человек не участвует ни в одном сборе.")

    await safe_edit(
        bot, chat_id, message_id, "\n".join(lines), families_kb(chat_id, 0)
    )


async def handle_callback(query, context: ContextTypes.DEFAULT_TYPE,
                          chat_id: int, data: str) -> None:
    bot = context.bot
    message = query.message
    message_id = message.message_id
    parts = data.split(":")

    if data == "m":
        await query.answer()
        await safe_edit(bot, chat_id, message_id, ADMIN_PANEL_TEXT, main_menu_kb())
        return

    if data == "x":
        await query.answer()
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass
        return

    if parts[0] == "cl":
        page = _to_int(parts[1]) if len(parts) > 1 else 0
        await query.answer()
        await show_collections(bot, chat_id, message_id, page)
        return

    if parts[0] == "c" and len(parts) >= 3:
        cid, page = _to_int(parts[1]), _to_int(parts[2])
        await query.answer()
        await show_collection_view(bot, chat_id, message_id, cid, page)
        return

    if parts[0] == "t" and len(parts) >= 4:
        cid, page, mid = _to_int(parts[1]), _to_int(parts[2]), _to_int(parts[3])
        changed = toggle_member(mid, chat_id)
        if not changed:
            await query.answer("Участник не найден", show_alert=True)
            return
        await query.answer(
            "✅ Отмечено" if changed["paid"] else "↩️ Снято с оплаты"
        )
        await show_collection_view(bot, chat_id, message_id, cid, page)
        await refresh_debtors(context, chat_id)
        return

    if parts[0] in ("pa", "pu") and len(parts) >= 2:
        cid = _to_int(parts[1])
        page = _to_int(parts[2]) if len(parts) > 2 else 0
        mark = parts[0] == "pa"
        count = set_collection_paid(cid, chat_id, mark)
        await query.answer(
            (f"✅ Отмечено: {count}" if mark else f"↩️ Снято: {count}")
        )
        await show_collection_view(bot, chat_id, message_id, cid, page)
        await refresh_debtors(context, chat_id)
        return

    if parts[0] == "dc" and len(parts) >= 2:
        cid = _to_int(parts[1])
        col = get_collection(cid, chat_id)
        if not col:
            await query.answer("Сбор не найден", show_alert=True)
            return
        await query.answer()
        text = (
            f"🗑 *Удалить сбор #{col['num']} «{md_escape(col['title'])}»?*\n\n"
            "Все отметки этого сбора тоже будут удалены.\n"
            "_Номера остальных сборов пересчитаются без дыр._"
        )
        rows = [
            [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"dy:{cid}")],
            [InlineKeyboardButton("⬅️ Отмена", callback_data=f"c:{cid}:0")],
        ]
        await safe_edit(bot, chat_id, message_id, text, kb(rows))
        return

    if parts[0] == "dy" and len(parts) >= 2:
        cid = _to_int(parts[1])
        result = delete_collection(cid, chat_id)
        if not result:
            await query.answer("Сбор не найден", show_alert=True)
            return
        title, num, members_count = result
        await query.answer(f"🗑 Сбор #{num} удалён")
        await show_collections(bot, chat_id, message_id, 0)
        await refresh_debtors(context, chat_id)
        log.info("Удалён сбор #%s «%s» (%s участников)", num, title, members_count)
        return

    if data == "dv":
        await query.answer("Обновляю список")
        await send_debtors(context, chat_id)
        return

    if parts[0] == "af":
        page = _to_int(parts[1]) if len(parts) > 1 else 0
        await query.answer()
        await show_families_picker(bot, chat_id, message_id, page)
        return

    if parts[0] == "as" and len(parts) >= 2:
        await query.answer()
        await show_family_confirm(bot, chat_id, message_id, _to_int(parts[1]))
        return

    if parts[0] == "ay" and len(parts) >= 2:
        await query.answer("Отмечаю…")
        await apply_family_everywhere(bot, chat_id, message_id, _to_int(parts[1]))
        await refresh_debtors(context, chat_id)
        return

    if data == "fl":
        await query.answer()
        fams = get_families(chat_id)
        if fams:
            text = (
                f"👥 *Фамилии этого чата* ({len(fams)}):\n\n"
                + md_escape(", ".join(fams))
                + "\n\n_Изменить список: `/setfamilies`_"
            )
        else:
            text = "👥 Список пуст. Задайте: `/setfamilies`"
        await safe_edit(bot, chat_id, message_id, text, main_menu_kb())
        return

    if data == "nw":
        await query.answer()
        text = (
            "🆕 *Новый сбор*\n\n"
            "Отправьте команду:\n"
            "`/new Подарок 300`\n"
            "`/new Подарок | 300`\n"
            "`/new Подарок` — без суммы\n\n"
            "В сбор попадут все фамилии из списка чата."
        )
        await safe_edit(bot, chat_id, message_id, text, main_menu_kb())
        return

    await query.answer("Кнопка устарела")


def _to_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""

    if not is_admin(query.from_user.id):
        await query.answer("⛔ Только администратор", show_alert=True)
        return

    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        await query.answer("Эта кнопка работает только в чате", show_alert=True)
        return

    try:
        await handle_callback(query, context, chat_id, data)
    except Exception:
        log.exception("Ошибка в обработке кнопки %r", data)
        try:
            await query.answer("⚠️ Ошибка, попробуйте ещё раз", show_alert=True)
        except TelegramError:
            pass


# ======================================================================
# УТИЛИТЫ / ДОСТУП
# ======================================================================

def is_admin(user_id: int) -> bool:
    return (not ADMIN_IDS) or (user_id in ADMIN_IDS)


async def delete_later(message, seconds: int = AUTO_DELETE_SECONDS) -> None:
    if not seconds or seconds <= 0:
        return
    try:
        await asyncio.sleep(seconds)
        await message.delete()
    except Exception:
        pass


async def reply_auto(update: Update, text: str, **kwargs):
    msg = await update.message.reply_text(text, **kwargs)
    asyncio.create_task(delete_later(msg, AUTO_DELETE_SECONDS))
    return msg


async def ensure_admin(update: Update) -> bool:
    if is_admin(update.effective_user.id):
        return True
    await reply_auto(update, "⛔ Только администратор.")
    return False


def parse_new_collection_text(body: str) -> tuple[str, int]:
    body = body.strip()
    if not body:
        return body, 0

    if "|" in body:
        left, right = body.rsplit("|", 1)
        nums = re.sub(r"[^\d]", "", right)
        return left.strip(), int(nums) if nums else 0

    match = re.match(
        r"^(.*?)\s+(\d+)\s*(?:руб(?:лей|ля|\.)?|₽|р\.?)?\s*$", body, re.IGNORECASE
    )
    if match:
        return match.group(1).strip(), int(match.group(2))

    return body, 0


# --- Разбор отметки об оплате ---
# Каждая запись ОБЯЗАТЕЛЬНО содержит маркер: +, скинул, оплатил, перевёл, отправил, заплатил
PAID_ITEM_RE = re.compile(
    r"^\s*([А-ЯЁA-Zа-яёa-z][А-ЯЁA-Zа-яёa-z\-']{1,40})\s*"
    r"(?:\+|\s+(?:скинул[аи]?|оплатил[а]?|перев[её]л[а]?|отправил[а]?|заплатил[а]?))"
    r"(?:\s*(?:в\s+сборе|сборе|#)\s*(\d+))?\s*$",
    re.IGNORECASE,
)


def parse_paid_message(text: str) -> list[tuple[str, Optional[int]]]:
    if not text or not text.strip():
        return []
    results = []
    for part in re.split(r"[\n,;]+", text):
        part = part.strip()
        if not part:
            continue
        match = PAID_ITEM_RE.match(part)
        if match:
            family = match.group(1).strip()
            cid = int(match.group(2)) if match.group(2) else None
            results.append((family, cid))
    return results


_pending_clear: dict[int, bool] = {}


# ======================================================================
# КОМАНДЫ
# ======================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_adm = is_admin(update.effective_user.id)
    text = (
        "👋 Привет! Я бот для сбора средств.\n\n"
        f"{'👑 Вы администратор — доступны кнопки /admin.' if is_adm else ''}\n\n"
        "*Для участников:*\n"
        "Напишите «Фамилия+», «Фамилия скинул», «Фамилия оплатил» — отмечу оплату.\n"
        "Можно сразу несколько:\n"
        "`Егай+`\n`Балакин+`\n`Бойков скинул`\n\n"
        "Можно указать сбор: «Фамилия + #2».\n\n"
        "«Список должников» — все сборы с итогами.\n"
        "«Список должников #2» — только по сбору №2.\n\n"
        "*Команды администратора:*\n"
        "/admin — панель с кнопками\n"
        "/setfamilies — задать список фамилий\n"
        "/families — показать список фамилий\n"
        "/new <описание> [сумма] — новый сбор\n"
        "/all — все сборы в этом чате\n"
        "/check <Фамилия> — где числится фамилия\n"
        "/unpay <Фамилия> [#N] — вернуть в должники\n"
        "/delcollection #N — удалить сбор по номеру\n"
        "/clearall — удалить все сборы в этом чате\n"
        "/bindold — привязать старые сборы к этому чату\n"
        "/transfer_to <chat_id> — перенести сборы в другой чат\n"
        "/export_db — скачать файл базы данных\n"
        "/restart — перезапустить бота\n\n"
        "_Номера сборов всегда идут подряд: #1, #2, #3…_"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb() if is_adm else None,
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    await update.message.reply_text(
        ADMIN_PANEL_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(),
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Ваш user_id: `{update.effective_user.id}`\n"
        f"Chat id: `{update.effective_chat.id}`\n"
        f"Тип чата: `{update.effective_chat.type}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_setfamilies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    body = parts[1] if len(parts) > 1 else ""
    if not body and update.message.reply_to_message:
        body = update.message.reply_to_message.text or ""

    names = [n.strip() for n in re.split(r"[\n,;]+", body) if n.strip()]
    seen, unique = set(), []
    for name in names:
        key = norm_name(name)
        if key and key not in seen:
            seen.add(key)
            unique.append(name)

    if not unique:
        await update.message.reply_text(
            "Пришлите список после команды, например:\n\n"
            "/setfamilies\nИванов\nПетров\nСидоров\n\n"
            "или в одну строку через запятую."
        )
        return

    set_families(chat_id, unique)
    await reply_auto(update, f"✅ Сохранено {len(unique)} фамилий:\n" + ", ".join(unique))


async def cmd_families(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    fams = get_families(chat_id)
    if not fams:
        await update.message.reply_text("Список пуст. Задайте через /setfamilies")
        return
    text = f"👥 Список фамилий этого чата ({len(fams)}):\n" + md_escape(", ".join(fams))
    for part in split_long(text):
        await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)


async def start_new_collection(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               title: str, amount: int = 0) -> None:
    chat_id = update.effective_chat.id
    families = get_families(chat_id)
    if not families:
        await update.message.reply_text(
            "⚠️ Сначала задайте список фамилий: /setfamilies"
        )
        return

    cid = create_collection(chat_id, title, families, amount)
    col = get_collection(cid, chat_id)
    listing = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(families))
    amount_line = f"\n💰 Сумма: *{amount} ₽* с человека\n" if amount else "\n"
    text = (
        f"📢 *Новый сбор #{col['num']}:* {md_escape(title)}\n{amount_line}\n"
        f"👥 Кто участвует:\n{md_escape(listing)}\n\n"
        f"Когда сдадите — напишите «Фамилия+» или «Фамилия скинул»."
    )
    parts = split_long(text)
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        await update.message.reply_text(
            part,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=(
                kb([[InlineKeyboardButton("✅ Отмечать сдавших",
                                          callback_data=f"c:{cid}:0")]])
                if last else None
            ),
        )
    await refresh_debtors(context, chat_id)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Укажите описание и сумму:\n"
            "`/new Сбор на подарок 300`\n"
            "`/new Сбор на подарок | 300`\n"
            "`/new Сбор на подарок` — без суммы",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    title, amount = parse_new_collection_text(parts[1].strip())
    await start_new_collection(update, context, title, amount)


async def cmd_debtors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    match = re.search(r"#?\s*(\d+)", text)
    if not match:
        await update.message.reply_text(
            "Использование: `/debtors 2`\nНомера сборов смотрите в `/all`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    col = collection_by_number(chat_id, int(match.group(1)))
    if not col:
        await reply_auto(
            update, f"⚠️ Сбор #{match.group(1)} не найден в этом чате."
        )
        return
    for part in split_long(render_collection_debtors(col)):
        await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    chat_id = update.effective_chat.id
    cols = fetch_collections(chat_id)
    if not cols:
        await reply_auto(update, "Сборов в этом чате пока нет.")
        return
    for part in split_long(render_all(chat_id)):
        await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await reply_auto(update, "Использование: /check <Фамилия>")
        return
    query = norm_name(parts[1])

    found = []
    for col in fetch_collections(chat_id):
        for member in get_members(col["id"]):
            if query in norm_name(member["family"]):
                status = "🟢 активный" if col["active"] else "⚪ завершён"
                if member["paid"]:
                    mark = "✅ оплачено"
                else:
                    mark = f"❌ не оплачено ({col['amount']} ₽)" if col["amount"] else "❌ не оплачено"
                found.append(
                    f"• #{col['num']} «{md_escape(col['title'])}» ({status}) — "
                    f"{md_escape(member['family'])}: {mark}"
                )

    if not found:
        await reply_auto(
            update, f"❌ Фамилия, содержащая «{parts[1].strip()}», не найдена."
        )
        return

    fams_match = [f for f in get_families(chat_id) if query in norm_name(f)]
    msg = "🔎 *Результаты поиска:*\n\n" + "\n".join(found)
    if fams_match:
        msg += "\n\n📋 В списке фамилий: " + md_escape(", ".join(fams_match))
    else:
        msg += "\n\n📋 В списке фамилий совпадений нет."
    for part in split_long(msg):
        await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)


async def cmd_unpay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Использование:\n"
            "• `/unpay Балакин` — в активном сборе\n"
            "• `/unpay Балакин #2` — в сборе №2",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    body = parts[1].strip()
    num = None
    family = body
    match = re.match(r"^(.*?)\s*#\s*(\d+)\s*$", body)
    if match:
        family = match.group(1).strip()
        num = int(match.group(2))

    if num is None:
        col = get_active_collection(chat_id)
        if not col:
            await reply_auto(update, "Нет активного сбора.")
            return
    else:
        col = collection_by_number(chat_id, num)
        if not col:
            await reply_auto(update, f"⚠️ Сбор #{num} не найден.")
            return

    status, name = unmark_paid(col["id"], chat_id, family)
    if status == "ok":
        await reply_auto(
            update,
            f"↩️ {name} возвращён в должники по «{col['title']}» (#{col['num']}).",
        )
        await refresh_debtors(context, chat_id)
    elif status == "already_unpaid":
        await reply_auto(
            update, f"ℹ️ «{name}» и так в должниках по «{col['title']}»."
        )
    else:
        await reply_auto(update, f"⚠️ «{family}» не найден в сборе «{col['title']}».")


async def cmd_delcollection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    match = re.search(r"#?\s*(\d+)", text)
    if not match:
        await reply_auto(update, "Использование: /delcollection #2")
        return
    num = int(match.group(1))
    col = collection_by_number(chat_id, num)
    if not col:
        await reply_auto(update, f"⚠️ Сбор #{num} не найден.")
        return
    result = delete_collection(col["id"], chat_id)
    if not result:
        await reply_auto(update, f"⚠️ Сбор #{num} не найден.")
        return
    title, deleted_num, members_count = result
    await reply_auto(
        update,
        f"🗑 Сбор #{deleted_num} «{title}» удалён ({members_count} участников).\n"
        f"Номера остальных сборов пересчитаны.",
    )
    await refresh_debtors(context, chat_id)


async def cmd_clearall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    chat_id = update.effective_chat.id
    cols = fetch_collections(chat_id)
    if not cols:
        await reply_auto(update, "В этом чате нет сборов.")
        return

    _pending_clear[chat_id] = True
    listing = "\n".join(f"• #{c['num']} — {c['title']}" for c in cols)
    await update.message.reply_text(
        f"⚠️ *Удалить ВСЕ сборы в этом чате?*\n\n"
        f"{md_escape(listing)}\n\n"
        f"Будет удалено: *{len(cols)}* сбора/сборов.\n"
        f"Список фамилий (`/families`) останется.\n\n"
        f"Напишите *`да`* для подтверждения или что угодно другое для отмены.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_bindold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    chat_id = update.effective_chat.id
    col_n, fam_n = bind_old_data_to_chat(chat_id)
    await reply_auto(
        update, f"📦 Привязано к чату: сборов — {col_n}, фамилий — {fam_n}."
    )


async def cmd_transfer_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Использование: `/transfer_to <chat_id>`\n\n"
            "Пример: `/transfer_to -1001234567890`\n\n"
            "Узнать chat_id группы: отправьте в ней `/id`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        to_chat_id = int(parts[1].strip())
    except ValueError:
        await reply_auto(update, "⚠️ chat_id должен быть числом, например -1001234567890.")
        return

    from_chat_id = update.effective_chat.id
    if from_chat_id == to_chat_id:
        await reply_auto(update, "⚠️ Это тот же самый чат.")
        return

    col_n, fam_n = transfer_data(from_chat_id, to_chat_id)
    if col_n == 0 and fam_n == 0:
        await reply_auto(update, "⚠️ В этом чате нечего переносить.")
        return

    await reply_auto(
        update,
        f"📦 Перенесено в чат `{to_chat_id}`:\n"
        f"• сборов: {col_n}\n"
        f"• фамилий: {fam_n}\n\n"
        f"Проверьте командой `/all` в целевом чате.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_export_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not os.path.exists(DB_PATH):
        await reply_auto(update, "⚠️ Файл базы не найден.")
        return
    try:
        try:
            path = backup_db("export") or DB_PATH
        except Exception:
            log.exception("Не удалось сделать копию перед экспортом")
            path = DB_PATH
        size = os.path.getsize(path)
        await update.message.reply_text(
            f"📦 Отправляю файл базы данных…\nРазмер: {size} байт."
        )
        with open(path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="collections_backup.db",
                caption="💾 Резервная копия базы данных бота",
            )
    except Exception as exc:
        log.exception("Ошибка экспорта базы")
        await reply_auto(update, f"⚠️ Ошибка: {exc}")


def _validate_db_file(path: str) -> bool:
    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
        return {"collections", "members"} <= names
    except sqlite3.Error:
        return False


async def cmd_import_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Восстановление базы из файла: ответьте на файл .db командой /import_db."""
    if not await ensure_admin(update):
        return
    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await reply_auto(
            update, "Использование: ответьте на файл базы (.db) командой /import_db"
        )
        return

    tmp = f"{DB_PATH}.import.tmp"
    try:
        tg_file = await context.bot.get_file(reply.document.file_id)
        await tg_file.download_to_drive(tmp)
        if not _validate_db_file(tmp):
            os.remove(tmp)
            await reply_auto(update, "⚠️ Это не база этого бота (нет таблиц collections/members).")
            return

        backup_db("before-import")
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = DB_PATH + suffix
            if os.path.exists(path):
                os.remove(path)
        shutil.move(tmp, DB_PATH)
        init_db()

        cols = fetch_collections(update.effective_chat.id)
        paid = sum(1 for c in cols for m in get_members(c["id"]) if m["paid"])
        await reply_auto(
            update,
            f"✅ База восстановлена.\nСборов: {len(cols)}\nОтметок об оплате: {paid}",
        )
    except Exception as exc:
        log.exception("Ошибка импорта базы")
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        await reply_auto(update, f"⚠️ Не удалось восстановить базу: {exc}")


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    await update.message.reply_text("🔄 Перезапускаю бота…")
    log.info("Bot restart requested by admin")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)


# ======================================================================
# ОБРАБОТКА ОБЫЧНЫХ СООБЩЕНИЙ
# ======================================================================

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    lower = text.lower()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    log.info("CHAT_ID=%s USER=%s TEXT=%s", chat_id, user_id, text)

    # 0) Подтверждение удаления всех сборов
    if _pending_clear.get(chat_id):
        del _pending_clear[chat_id]
        if lower == "да":
            if not is_admin(user_id):
                await reply_auto(update, "⛔ Только администратор.")
                return
            n = delete_all_collections(chat_id)
            await reply_auto(update, f"🗑 Удалено сборов: {n}. Фамилии не тронуты.")
            await refresh_debtors(context, chat_id)
        else:
            await reply_auto(update, "❌ Отменено.")
        return

    # 0.1) Панель администратора
    if lower in ("меню", "панель", "админ", "админка", "кнопки", "⚙️ панель"):
        if not is_admin(user_id):
            return
        await update.message.reply_text(
            ADMIN_PANEL_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(),
        )
        return

    # 1) Список должников #N — по одному сбору
    match_dd = re.match(
        r"^(?:список\s+должников|должники|кто\s+не\s+сдал)\s*#?\s*(\d+)\s*$", lower
    )
    if match_dd:
        col = collection_by_number(chat_id, int(match_dd.group(1)))
        if not col:
            await reply_auto(update, f"⚠️ Сбор #{match_dd.group(1)} не найден.")
            return
        for part in split_long(render_collection_debtors(col)):
            await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)
        return

    # 2) Список должников / сдавших — общий
    if lower in ("список должников", "должники", "кто не сдал", "список сдавших"):
        if lower == "список сдавших":
            col = get_active_collection(chat_id)
            if not col:
                await reply_auto(update, "Нет активного сбора.")
                return
            paid = [m["family"] for m in get_members(col["id"]) if m["paid"]]
            suffix = f" — {col['amount']} ₽" if col["amount"] else ""
            await update.message.reply_text(
                f"✅ По #{col['num']} «{col['title']}» сдали{suffix}:\n"
                + ("\n".join(f"• {f}" for f in paid) or "—"),
            )
            return
        await send_debtors(context, chat_id)
        return

    # 3) Новый сбор текстом
    match = re.match(r"^(?:новый\s+)?сбор\s+(.+)$", text, re.IGNORECASE)
    if match:
        if not is_admin(user_id):
            await reply_auto(update, "⛔ Только администратор может создавать сборы.")
            return
        title, amount = parse_new_collection_text(match.group(1).strip())
        await start_new_collection(update, context, title, amount)
        return

    # 4) Отметка оплаты — одна или несколько фамилий (только с маркером!)
    paid_items = parse_paid_message(text)
    if paid_items:
        await handle_paid_items(update, context, paid_items)
        return


async def handle_paid_items(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            paid_items: list[tuple[str, Optional[int]]]) -> None:
    chat_id = update.effective_chat.id

    # --- Одиночный вариант ---
    if len(paid_items) == 1:
        family, explicit_num = paid_items[0]

        if explicit_num:
            col = collection_by_number(chat_id, explicit_num)
            if not col:
                await reply_auto(update, f"⚠️ Сбор #{explicit_num} не найден.")
                return
            status, name = mark_paid(col["id"], chat_id, family)
            if status == "ok":
                await reply_auto(
                    update, f"✅ {name} — оплата отмечена (#{col['num']})."
                )
                await refresh_debtors(context, chat_id)
            elif status == "already":
                await reply_auto(
                    update, f"ℹ️ {name} уже отмечен (#{col['num']})."
                )
            else:
                await reply_auto(
                    update, f"⚠️ «{family}» не найден в сборе #{col['num']}."
                )
            return

        active = get_active_collection(chat_id)
        if active:
            status, name = mark_paid(active["id"], chat_id, family)
            if status == "ok":
                await reply_auto(
                    update, f"✅ {name} — оплата отмечена по «{active['title']}»."
                )
                await refresh_debtors(context, chat_id)
                return
            if status == "already":
                await reply_auto(
                    update, f"ℹ️ {name} уже отмечен по «{active['title']}»."
                )
                return

        past = find_unpaid_in_past(chat_id, family)
        if len(past) == 1:
            col, member = past[0]
            set_member_paid(member["id"], True)
            await reply_auto(
                update,
                f"✅ {member['family']} — оплата отмечена в прошлом сборе "
                f"«{col['title']}» (#{col['num']}).",
            )
            await refresh_debtors(context, chat_id)
            return
        if len(past) > 1:
            listing = "\n".join(f"• #{c['num']} — {c['title']}" for c, _ in past)
            await reply_auto(
                update,
                f"⚠️ Несколько прошлых сборов с «{family}»:\n{listing}\n"
                f"Уточните: «{family} + #{past[0][0]['num']}»",
            )
            return

        if active:
            await reply_auto(
                update,
                f"⚠️ «{family}» не найден в текущем сборе и в прошлых. "
                f"Проверьте написание или /check.",
            )
        else:
            await reply_auto(update, f"⚠️ «{family}» не найден в прошлых сборах.")
        return

    # --- Массовый вариант ---
    active = get_active_collection(chat_id)
    target = None
    if not any(num for _, num in paid_items) and active:
        target = active

    ok_list, already_list, notfound_list, past_multi = [], [], [], []

    for family, num in paid_items:
        if num:
            col = collection_by_number(chat_id, num)
            if not col:
                notfound_list.append(f"{family} (#{num} не найден)")
                continue
            status, name = mark_paid(col["id"], chat_id, family)
        elif target:
            status, name = mark_paid(target["id"], chat_id, family)
        else:
            past = find_unpaid_in_past(chat_id, family)
            if len(past) == 1:
                col, member = past[0]
                set_member_paid(member["id"], True)
                status, name = "ok", member["family"]
            elif len(past) > 1:
                past_multi.append(family)
                continue
            else:
                status, name = "not_found", None

        if status == "ok":
            ok_list.append(name or family)
        elif status == "already":
            already_list.append(name or family)
        else:
            notfound_list.append(family)

    lines = []
    if target:
        lines.append(f"💸 Отметки по сбору «{target['title']}»:")
    else:
        lines.append("💸 Отметки оплаты:")
    if ok_list:
        lines.append("✅ Отмечены: " + ", ".join(ok_list))
    if already_list:
        lines.append("ℹ️ Уже были: " + ", ".join(already_list))
    if notfound_list:
        lines.append("⚠️ Не найдены: " + ", ".join(notfound_list))
    if past_multi:
        lines.append("⚠️ Требуют уточнения (#N): " + ", ".join(past_multi))

    await reply_auto(update, "\n".join(lines))
    if ok_list:
        await refresh_debtors(context, chat_id)


# ======================================================================
# ЗАПУСК
# ======================================================================

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Ошибка при обработке обновления", exc_info=context.error)


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("setfamilies", cmd_setfamilies))
    app.add_handler(CommandHandler("families", cmd_families))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("debtors", cmd_debtors))
    app.add_handler(CommandHandler("unpay", cmd_unpay))
    app.add_handler(CommandHandler("delcollection", cmd_delcollection))
    app.add_handler(CommandHandler("clearall", cmd_clearall))
    app.add_handler(CommandHandler("bindold", cmd_bindold))
    app.add_handler(CommandHandler("transfer_to", cmd_transfer_to))
    app.add_handler(CommandHandler("export_db", cmd_export_db))
    app.add_handler(CommandHandler("import_db", cmd_import_db))
    app.add_handler(CommandHandler("restart", cmd_restart))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    if not BOT_TOKEN:
        print("❌ Не задан BOT_TOKEN. Укажите переменную окружения BOT_TOKEN.")
        sys.exit(1)

    init_db()
    log.info("База данных: %s", DB_PATH)
    log.info("Администраторы: %s", ADMIN_IDS)

    app = build_app()
    print("Бот запущен…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
