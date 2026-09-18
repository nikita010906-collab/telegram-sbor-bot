import sqlite3
import logging
import re
import os
import sys
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [1920218354]   # ваш user_id
DB_PATH = "collections.db"
# ===================================

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)


# ---------- БД ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        active INTEGER DEFAULT 1,
        amount INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collection_id INTEGER NOT NULL,
        family TEXT NOT NULL,
        paid INTEGER DEFAULT 0,
        paid_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS families (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS family_contacts (
        family TEXT UNIQUE NOT NULL,
        username TEXT,
        user_id INTEGER,
        full_name TEXT
    )""")

    # Миграция: добавить колонку amount, если её нет (для старых баз)
    cols = [row[1] for row in c.execute("PRAGMA table_info(collections)").fetchall()]
    if "amount" not in cols:
        c.execute("ALTER TABLE collections ADD COLUMN amount INTEGER DEFAULT 0")

    conn.commit()
    conn.close()


def get_families():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT name FROM families ORDER BY name").fetchall()
    conn.close()
    return [r[0] for r in rows]


def set_families(names):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM families")
    for n in names:
        c.execute("INSERT OR IGNORE INTO families (name) VALUES (?)", (n,))
    conn.commit()
    conn.close()


def create_collection(title, families, amount=0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE collections SET active=0 WHERE active=1")
    c.execute("INSERT INTO collections (title, active, amount) VALUES (?, 1, ?)", (title, amount))
    cid = c.lastrowid
    for f in families:
        c.execute("INSERT INTO members (collection_id, family) VALUES (?, ?)", (cid, f))
    conn.commit()
    conn.close()
    return cid


def get_active_collection():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, title, amount FROM collections WHERE active=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row


def get_collection(cid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, title, amount FROM collections WHERE id=?", (cid,)
    ).fetchone()
    conn.close()
    return row


def get_members(cid):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT family, paid FROM members WHERE collection_id=? ORDER BY family", (cid,)
    ).fetchall()
    conn.close()
    return rows


def mark_paid(cid, family):
    """Регистронезависимо для любого Юникода (включая кириллицу)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, family, paid FROM members WHERE collection_id=?", (cid,)
    ).fetchall()
    target = family.strip().lower()
    matched = None
    for r in rows:
        if r[1].strip().lower() == target:
            matched = r
            break
    if not matched:
        conn.close()
        return "not_found", None
    if matched[2]:
        conn.close()
        return "already", matched[1]
    c.execute("UPDATE members SET paid=1, paid_at=CURRENT_TIMESTAMP WHERE id=?", (matched[0],))
    conn.commit()
    conn.close()
    return "ok", matched[1]


def unmark_paid(cid, family):
    """Снимает отметку об оплате (возвращает в должники)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, family, paid FROM members WHERE collection_id=?", (cid,)
    ).fetchall()
    target = family.strip().lower()
    matched = None
    for r in rows:
        if r[1].strip().lower() == target:
            matched = r
            break
    if not matched:
        conn.close()
        return "not_found", None
    if not matched[2]:
        conn.close()
        return "already_unpaid", matched[1]
    c.execute("UPDATE members SET paid=0, paid_at=NULL WHERE id=?", (matched[0],))
    conn.commit()
    conn.close()
    return "ok", matched[1]


def mark_paid_by_member_id(mid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute("SELECT family, paid FROM members WHERE id=?", (mid,)).fetchone()
    if not row:
        conn.close()
        return "not_found", None
    if row[1]:
        conn.close()
        return "already", row[0]
    c.execute("UPDATE members SET paid=1, paid_at=CURRENT_TIMESTAMP WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return "ok", row[0]


def find_unpaid_in_past(family):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT c.id, c.title, m.id, m.family
        FROM members m
        JOIN collections c ON c.id = m.collection_id
        WHERE c.active = 0 AND m.paid = 0
    """).fetchall()
    conn.close()
    target = family.strip().lower()
    return [r for r in rows if r[3].strip().lower() == target]


def get_all_collections():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, title, created_at, active, amount FROM collections ORDER BY id"
    ).fetchall()
    result = []
    for r in rows:
        members = c.execute(
            "SELECT family, paid FROM members WHERE collection_id=? ORDER BY family", (r[0],)
        ).fetchall()
        result.append({
            "id": r[0], "title": r[1], "created_at": r[2],
            "active": r[3], "amount": r[4] or 0, "members": members,
        })
    conn.close()
    return result


def get_all_debtors():
    """Возвращает список (cid, title, amount, family) для всех неуплаченных."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT c.id, c.title, c.amount, m.family
        FROM members m
        JOIN collections c ON c.id = m.collection_id
        WHERE m.paid = 0
        ORDER BY c.id DESC, m.family
    """).fetchall()
    conn.close()
    return [(r[0], r[1], r[2] or 0, r[3]) for r in rows]


# ---------- Контакты (упоминания) ----------
def save_contact(family, username, user_id, full_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO family_contacts (family, username, user_id, full_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(family) DO UPDATE SET
            username=excluded.username,
            user_id=excluded.user_id,
            full_name=excluded.full_name
    """, (family, username, user_id, full_name))
    conn.commit()
    conn.close()


def get_contact(family):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT username, user_id, full_name FROM family_contacts WHERE LOWER(family)=LOWER(?)",
        (family,),
    ).fetchone()
    conn.close()
    return row


def get_all_contacts():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT family, username, user_id, full_name FROM family_contacts ORDER BY family"
    ).fetchall()
    conn.close()
    return rows


def delete_contact(family):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM family_contacts WHERE LOWER(family)=LOWER(?)", (family,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def format_mention(family):
    """Возвращает @username, ссылку-упоминание или просто фамилию."""
    contact = get_contact(family)
    if not contact:
        return family
    username, user_id, full_name = contact
    if username:
        return f"@{username}"
    if user_id:
        return f"[{family}](tg://user?id={user_id})"
    return family


# ---------- Утилиты ----------
def is_admin(user_id: int) -> bool:
    return (not ADMIN_IDS) or (user_id in ADMIN_IDS)


def split_long(text: str, limit: int = 3800):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        parts.append(cur)
    return parts


def format_money(amount):
    """Форматирует сумму. 0 → '—', иначе '300 ₽'."""
    if not amount:
        return "—"
    return f"{amount} ₽"


def parse_new_collection_text(body):
    """Извлекает название и сумму из строки.
    Поддерживает:
      'Сбор на подарок 300'      → ('Сбор на подарок', 300)
      'Сбор на подарок 300 руб'  → ('Сбор на подарок', 300)
      'Сбор на подарок | 300'    → ('Сбор на подарок', 300)
      'Сбор на подарок'          → ('Сбор на подарок', 0)
    """
    body = body.strip()
    if not body:
        return body, 0

    # Явный разделитель |
    if "|" in body:
        left, right = body.rsplit("|", 1)
        nums = re.sub(r"[^\d]", "", right)
        try:
            amount = int(nums) if nums else 0
        except ValueError:
            amount = 0
        return left.strip(), amount

    # Число в конце строки
    m = re.match(r"^(.*?)\s+(\d+)\s*(?:руб(?:лей|ля|\.)?|₽|р\.?)?\s*$", body, re.IGNORECASE)
    if m:
        return m.group(1).strip(), int(m.group(2))

    return body, 0


PAID_RE = re.compile(
    r"^\s*([А-ЯЁA-Zа-яёa-z][А-ЯЁA-Zа-яёa-z\-']{1,40})\s*"
    r"(скинул[аи]?|оплатил[а]?|перев[её]л[а]?|отправил[а]?|заплатил[а]?|\+)"
    r"(?:\s*(?:в\s+сборе|сборе|#)\s*(\d+))?\s*$",
    re.IGNORECASE,
)


# ---------- Команды ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привет! Я бот для сбора средств.\n\n"
        "*Команды администратора:*\n"
        "/setfamilies — задать список фамилий\n"
        "/families — показать список фамилий\n"
        "/new <описание> [сумма] — новый сбор. Пример: `/new Подарок 300`\n"
        "/all — все сборы с описанием и статусами\n"
        "/check <Фамилия> — где числится фамилия\n"
        "/unpay <Фамилия> [#N] — вернуть в должники\n"
        "/setuser <Фамилия> [@username] — привязать Telegram\n"
        "/unsetuser <Фамилия> — удалить привязку\n"
        "/users — список привязок\n"
        "/restart — перезапустить бота\n\n"
        "*Для участников:*\n"
        "Напишите «Фамилия скинул» или «Фамилия +» — отмечу оплату.\n"
        "Можно указать прошлый сбор: «Фамилия + #2».\n\n"
        "«Список должников» — покажу всех должников по всем сборам с итогами."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш user_id: `{update.effective_user.id}`\nChat id: `{update.effective_chat.id}`",
        parse_mode="Markdown",
    )


async def cmd_setfamilies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор может менять список.")
        return
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    body = parts[1] if len(parts) > 1 else ""
    if not body and update.message.reply_to_message:
        body = update.message.reply_to_message.text or ""

    names = [n.strip() for n in re.split(r"[\n,;]+", body) if n.strip()]
    seen, unique = set(), []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            unique.append(n)

    if not unique:
        await update.message.reply_text(
            "Пришлите список после команды, например:\n\n"
            "/setfamilies\nИванов\nПетров\nСидоров\n\n"
            "или в одну строку через запятую."
        )
        return

    set_families(unique)
    await update.message.reply_text(
        f"✅ Сохранено {len(unique)} фамилий:\n" + ", ".join(unique)
    )


async def cmd_families(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fams = get_families()
    if not fams:
        await update.message.reply_text("Список пуст. Задайте через /setfamilies")
        return
    await update.message.reply_text("👥 Список фамилий:\n" + ", ".join(fams))


async def start_new_collection(update: Update, title: str, amount: int = 0):
    families = get_families()
    if not families:
        await update.message.reply_text(
            "⚠️ Сначала задайте список фамилий: /setfamilies"
        )
        return
    cid = create_collection(title, families, amount)
    listing = "\n".join(f"{i+1}. {f}" for i, f in enumerate(families))
    amount_line = f"\n💰 Сумма: *{amount} ₽* с человека\n" if amount else "\n"
    await update.message.reply_text(
        f"📢 *Новый сбор #{cid}:* {title}\n{amount_line}\n"
        f"👥 Кто участвует:\n{listing}\n\n"
        f"Когда сдадите — напишите «Фамилия скинул» или «Фамилия +».",
        parse_mode="Markdown",
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор может создавать сборы.")
        return
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Укажите описание и сумму:\n"
            "`/new Сбор на подарок 300`\n"
            "`/new Сбор на подарок | 300`\n"
            "`/new Сбор на подарок` — без суммы",
            parse_mode="Markdown",
        )
        return
    title, amount = parse_new_collection_text(parts[1].strip())
    await start_new_collection(update, title, amount)


async def show_debtors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает должников по всем сборам + общий итог по людям."""
    debtors = get_all_debtors()
    if not debtors:
        await update.message.reply_text("🎉 Должников нет — все сдали!")
        return

    # Группируем по сборам
    by_collection = {}   # (cid, title, amount) -> [families]
    totals = {}          # family -> сумма долга
    order = []           # порядок сборов
    for cid, title, amount, family in debtors:
        key = (cid, title, amount)
        if key not in by_collection:
            by_collection[key] = []
            order.append(key)
        by_collection[key].append(family)
        totals[family] = totals.get(family, 0) + (amount or 0)

    lines = ["💸 *Список должников*\n"]
    grand_total = 0
    for key in order:
        cid, title, amount = key
        fams = by_collection[key]
        amount_str = f"по {amount} ₽" if amount else "сумма не указана"
        lines.append(f"📌 *#{cid} «{title}»* _{amount_str}_")
        for f in fams:
            mention = format_mention(f)
            money = f"{amount} ₽" if amount else "—"
            if mention.startswith("@"):
                lines.append(f"   • {mention} ({f}) — {money}")
            elif mention.startswith("["):
                lines.append(f"   • {mention} — {money}")
            else:
                lines.append(f"   • {f} — {money}")
            if amount:
                grand_total += amount
        lines.append("")

    lines.append("━━━━━━━━━━━━━━")
    lines.append("📊 *Итого по людям (по всем сборам):*")
    # сортируем по убыванию суммы
    for f, total in sorted(totals.items(), key=lambda x: (-x[1], x[0])):
        mention = format_mention(f)
        money = f"{total} ₽" if total else "—"
        if mention.startswith("@"):
            lines.append(f"• {mention} ({f}) — *{money}*")
        elif mention.startswith("["):
            lines.append(f"• {mention} — *{money}*")
        else:
            lines.append(f"• {f} — *{money}*")

    lines.append("")
    lines.append(f"💰 *Всего к сбору: {grand_total} ₽*")

    text = "\n".join(lines)
    for part in split_long(text):
        await update.message.reply_text(
            part, parse_mode="Markdown", disable_web_page_preview=True
        )


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    cols = get_all_collections()
    if not cols:
        await update.message.reply_text("Сборов пока нет.")
        return

    blocks = []
    for col in cols:
        paid = [f for f, p in col["members"] if p]
        unpaid = [f for f, p in col["members"] if not p]
        status = "🟢 активный" if col["active"] else "⚪ завершён"
        amount_str = f" • {col['amount']} ₽ с чел." if col["amount"] else ""
        b = f"*#{col['id']} — {col['title']}*\n"
        b += f"_Создан: {col['created_at']} • {status}{amount_str}_\n"
        b += f"✅ Сдали ({len(paid)}/{len(col['members'])}): {', '.join(paid) or '—'}\n"
        if unpaid:
            b += f"❌ Должники ({len(unpaid)}): {', '.join(unpaid)}"
        blocks.append(b)

    text = "\n\n".join(blocks)
    for part in split_long(text):
        await update.message.reply_text(part, parse_mode="Markdown")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Использование: /check <Фамилия>")
        return
    query = parts[1].strip().lower()

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT c.id, c.title, c.active, c.amount, m.family, m.paid
        FROM members m
        JOIN collections c ON c.id = m.collection_id
        ORDER BY c.id
    """).fetchall()
    conn.close()

    found = []
    for cid, title, active, amount, fam, paid in rows:
        if query in fam.lower():
            status = "🟢 активный" if active else "⚪ завершён"
            mark = "✅ оплачено" if paid else f"❌ не оплачено ({amount} ₽)"
            found.append(f"• #{cid} «{title}» ({status}) — {fam}: {mark}")

    if not found:
        await update.message.reply_text(
            f"❌ Фамилия, содержащая «{query}», не найдена ни в одном сборе."
        )
        return

    fams = get_families()
    fams_match = [f for f in fams if query in f.lower()]
    msg = "🔎 *Результаты поиска:*\n\n" + "\n".join(found)
    if fams_match:
        msg += "\n\n📋 В общем списке фамилий: " + ", ".join(fams_match)
    else:
        msg += "\n\n📋 В общем списке фамилий совпадений нет."
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_unpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Снять отметку об оплате (вернуть в должники).
    /unpay Фамилия       — в активном сборе
    /unpay Фамилия #N    — в конкретном сборе
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Использование:\n"
            "• `/unpay Балакин` — в активном сборе\n"
            "• `/unpay Балакин #2` — в сборе #2",
            parse_mode="Markdown",
        )
        return

    body = parts[1].strip()
    cid = None
    family = body

    m = re.match(r"^(.*?)\s*#\s*(\d+)\s*$", body)
    if m:
        family = m.group(1).strip()
        cid = int(m.group(2))

    if cid is None:
        active = get_active_collection()
        if not active:
            await update.message.reply_text("Нет активного сбора.")
            return
        cid, title = active[0], active[1]
    else:
        col = get_collection(cid)
        if not col:
            await update.message.reply_text(f"⚠️ Сбор #{cid} не найден.")
            return
        title = col[1]

    status, name = unmark_paid(cid, family)
    if status == "ok":
        await update.message.reply_text(
            f"↩️ {name} возвращён в должники по сбору «{title}» (#{cid})."
        )
    elif status == "already_unpaid":
        await update.message.reply_text(
            f"ℹ️ «{name}» и так числится в должниках по сбору «{title}»."
        )
    else:
        await update.message.reply_text(
            f"⚠️ «{family}» не найден в сборе «{title}»."
        )


async def cmd_setuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Привязать Telegram к фамилии.
    /setuser Бамбаев @rubogent
    /setuser Бамбаев   (ответом на сообщение человека)
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return

    text = update.message.text or ""
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        await update.message.reply_text(
            "Использование:\n"
            "• `/setuser Бамбаев @rubogent` — по @username\n"
            "• ответом на сообщение человека: `/setuser Бамбаев`",
            parse_mode="Markdown",
        )
        return

    family = parts[1].strip()

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        u = update.message.reply_to_message.from_user
        save_contact(family, u.username, u.id, u.full_name)
        await update.message.reply_text(
            f"✅ Фамилия «{family}» привязана к {u.full_name}"
            + (f" (@{u.username})" if u.username else "")
        )
        return

    if len(parts) < 3:
        await update.message.reply_text(
            "⚠️ Укажите @username или ответьте на сообщение человека."
        )
        return

    username = parts[2].strip().lstrip("@")
    save_contact(family, username, None, None)
    await update.message.reply_text(f"✅ Фамилия «{family}» привязана к @{username}")


async def cmd_unsetuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Использование: /unsetuser Бамбаев")
        return
    family = parts[1].strip()
    if delete_contact(family):
        await update.message.reply_text(f"🗑 Привязка для «{family}» удалена.")
    else:
        await update.message.reply_text(f"⚠️ Для «{family}» привязки не было.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    contacts = get_all_contacts()
    if not contacts:
        await update.message.reply_text(
            "Пока нет ни одной привязки.\n"
            "Как только кто-то напишет «Фамилия скинул», бот запомнит его аккаунт."
        )
        return
    lines = []
    for fam, username, user_id, full_name in contacts:
        if username:
            lines.append(f"• {fam} → @{username}")
        elif user_id:
            lines.append(f"• {fam} → {full_name or 'без username'} (id {user_id})")
        else:
            lines.append(f"• {fam} → (нет данных)")
    await update.message.reply_text(
        "🔗 *Привязки фамилий к Telegram:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    await update.message.reply_text("🔄 Перезапускаю бота... Через 5–15 секунд снова будет в строю.")
    logging.info("Bot restart requested by admin")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)


# ---------- Обработка обычных сообщений ----------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    lower = text.lower()

    # 1) Список должников / сдавших
    if lower in ("список должников", "должники", "кто не сдал", "список сдавших"):
        if lower == "список сдавших":
            active = get_active_collection()
            if not active:
                await update.message.reply_text("Нет активного сбора.")
                return
            cid, title, amount = active
            paid = [f for f, p in get_members(cid) if p]
            money = f" — {amount} ₽" if amount else ""
            await update.message.reply_text(
                f"✅ По «{title}» сдали{money}:\n"
                + ("\n".join(f"• {f}" for f in paid) or "—"),
            )
            return
        await show_debtors(update, context)
        return

    # 2) Новый сбор текстом (с суммой)
    m = re.match(r"^(?:новый\s+)?сбор\s+(.+)$", text, re.IGNORECASE)
    if m:
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Только администратор может создавать сборы.")
            return
        title, amount = parse_new_collection_text(m.group(1).strip())
        await start_new_collection(update, title, amount)
        return

    # 3) «Фамилия скинул/оплатил/+» и опционально «#N»
    m = PAID_RE.match(text)
    if m:
        family = m.group(1)
        explicit_cid = int(m.group(3)) if m.group(3) else None

        user = update.effective_user
        if user:
            save_contact(family.strip(), user.username, user.id, user.full_name)

        if explicit_cid:
            col = get_collection(explicit_cid)
            if not col:
                await update.message.reply_text(f"⚠️ Сбор #{explicit_cid} не найден.")
                return
            status, name = mark_paid(explicit_cid, family)
            if status == "ok":
                await update.message.reply_text(
                    f"✅ {name} — оплата отмечена по сбору «{col[1]}» (#{explicit_cid})."
                )
            elif status == "already":
                await update.message.reply_text(
                    f"ℹ️ {name} уже отмечен как оплативший по сбору «{col[1]}»."
                )
            else:
                await update.message.reply_text(
                    f"⚠️ «{family}» не найден в сборе «{col[1]}»."
                )
            return

        active = get_active_collection()
        if active:
            cid, title, amount = active
            status, name = mark_paid(cid, family)
            if status == "ok":
                await update.message.reply_text(
                    f"✅ {name} — оплата отмечена по сбору «{title}»."
                )
                return
            if status == "already":
                await update.message.reply_text(
                    f"ℹ️ {name} уже отмечен как оплативший по сбору «{title}»."
                )
                return

        past = find_unpaid_in_past(family)
        if len(past) == 1:
            pcid, ptitle, mid, pfam = past[0]
            status, name = mark_paid_by_member_id(mid)
            if status == "ok":
                await update.message.reply_text(
                    f"✅ {name} — оплата отмечена по прошлому сбору «{ptitle}» (#{pcid})."
                )
            else:
                await update.message.reply_text(
                    f"ℹ️ {name} уже отмечен по сбору «{ptitle}»."
                )
            return
        if len(past) > 1:
            listing = "\n".join(f"• #{c} — {t}" for c, t, _, _ in past)
            await update.message.reply_text(
                f"⚠️ Найдено несколько прошлых сборов с «{family}»:\n{listing}\n\n"
                f"Уточните, например: «{family} + #{past[0][0]}»"
            )
            return

        if active:
            await update.message.reply_text(
                f"⚠️ «{family}» не найден в текущем сборе «{active[1]}» и в прошлых сборах.\n"
                f"Проверьте написание или используйте /check."
            )
        else:
            await update.message.reply_text(
                f"⚠️ «{family}» не найден в прошлых сборах."
            )
        return


# ---------- Запуск ----------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("setfamilies", cmd_setfamilies))
    app.add_handler(CommandHandler("families", cmd_families))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("unpay", cmd_unpay))
    app.add_handler(CommandHandler("setuser", cmd_setuser))
    app.add_handler(CommandHandler("unsetuser", cmd_unsetuser))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("restart", cmd_restart))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    print("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()