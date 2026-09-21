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
        amount INTEGER DEFAULT 0,
        chat_id INTEGER DEFAULT 0
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
        chat_id INTEGER DEFAULT 0,
        name TEXT NOT NULL,
        UNIQUE(chat_id, name)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS family_contacts (
        family TEXT UNIQUE NOT NULL,
        username TEXT,
        user_id INTEGER,
        full_name TEXT
    )""")

    # --- МИГРАЦИИ для старых баз ---
    cols = [row[1] for row in c.execute("PRAGMA table_info(collections)").fetchall()]
    if "amount" not in cols:
        c.execute("ALTER TABLE collections ADD COLUMN amount INTEGER DEFAULT 0")
    if "chat_id" not in cols:
        c.execute("ALTER TABLE collections ADD COLUMN chat_id INTEGER DEFAULT 0")

    fam_cols = [row[1] for row in c.execute("PRAGMA table_info(families)").fetchall()]
    if "chat_id" not in fam_cols:
        c.execute("ALTER TABLE families RENAME TO families_old")
        c.execute("""CREATE TABLE families (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER DEFAULT 0,
            name TEXT NOT NULL,
            UNIQUE(chat_id, name)
        )""")
        c.execute("INSERT INTO families (chat_id, name) SELECT 0, name FROM families_old")
        c.execute("DROP TABLE families_old")

    conn.commit()
    conn.close()


# ---------- Фамилии (по чату) ----------
def get_families(chat_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT name FROM families WHERE chat_id=? ORDER BY name", (chat_id,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def set_families(chat_id, names):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM families WHERE chat_id=?", (chat_id,))
    for n in names:
        c.execute(
            "INSERT OR IGNORE INTO families (chat_id, name) VALUES (?, ?)",
            (chat_id, n),
        )
    conn.commit()
    conn.close()


# ---------- Сборы (по чату) ----------
def create_collection(chat_id, title, families, amount=0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE collections SET active=0 WHERE active=1 AND chat_id=?", (chat_id,))
    c.execute(
        "INSERT INTO collections (title, active, amount, chat_id) VALUES (?, 1, ?, ?)",
        (title, amount, chat_id),
    )
    cid = c.lastrowid
    for f in families:
        c.execute("INSERT INTO members (collection_id, family) VALUES (?, ?)", (cid, f))
    conn.commit()
    conn.close()
    return cid


def get_active_collection(chat_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, title, amount FROM collections WHERE active=1 AND chat_id=? "
        "ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    conn.close()
    return row


def get_collection(cid, chat_id=None):
    conn = sqlite3.connect(DB_PATH)
    if chat_id is None:
        row = conn.execute(
            "SELECT id, title, amount FROM collections WHERE id=?", (cid,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, title, amount FROM collections WHERE id=? AND chat_id=?",
            (cid, chat_id),
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


def find_unpaid_in_past(chat_id, family):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT c.id, c.title, m.id, m.family
        FROM members m
        JOIN collections c ON c.id = m.collection_id
        WHERE c.active = 0 AND m.paid = 0 AND c.chat_id = ?
    """, (chat_id,)).fetchall()
    conn.close()
    target = family.strip().lower()
    return [r for r in rows if r[3].strip().lower() == target]


def get_all_collections(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, title, created_at, active, amount FROM collections "
        "WHERE chat_id=? ORDER BY id",
        (chat_id,),
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


def get_all_debtors(chat_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT c.id, c.title, c.amount, m.family
        FROM members m
        JOIN collections c ON c.id = m.collection_id
        WHERE m.paid = 0 AND c.chat_id = ?
        ORDER BY c.id DESC, m.family
    """, (chat_id,)).fetchall()
    conn.close()
    return [(r[0], r[1], r[2] or 0, r[3]) for r in rows]


def delete_collection(cid, chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute(
        "SELECT id, title FROM collections WHERE id=? AND chat_id=?",
        (cid, chat_id),
    ).fetchone()
    if not row:
        conn.close()
        return None
    members_count = c.execute(
        "SELECT COUNT(*) FROM members WHERE collection_id=?", (cid,)
    ).fetchone()[0]
    c.execute("DELETE FROM members WHERE collection_id=?", (cid,))
    c.execute("DELETE FROM collections WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return row[1], members_count


def delete_all_collections(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    ids = [r[0] for r in c.execute(
        "SELECT id FROM collections WHERE chat_id=?", (chat_id,)
    ).fetchall()]
    if not ids:
        conn.close()
        return 0
    qmarks = ",".join("?" * len(ids))
    c.execute(f"DELETE FROM members WHERE collection_id IN ({qmarks})", ids)
    c.execute("DELETE FROM collections WHERE chat_id=?", (chat_id,))
    n = c.rowcount
    conn.commit()
    conn.close()
    return n


def bind_old_data_to_chat(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE collections SET chat_id=? WHERE chat_id=0", (chat_id,))
    col_n = c.rowcount
    c.execute("UPDATE families SET chat_id=? WHERE chat_id=0", (chat_id,))
    fam_n = c.rowcount
    conn.commit()
    conn.close()
    return col_n, fam_n


# ---------- Контакты ----------
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


def parse_new_collection_text(body):
    body = body.strip()
    if not body:
        return body, 0

    if "|" in body:
        left, right = body.rsplit("|", 1)
        nums = re.sub(r"[^\d]", "", right)
        try:
            amount = int(nums) if nums else 0
        except ValueError:
            amount = 0
        return left.strip(), amount

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

_pending_clear = {}


# ---------- Команды ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привет! Я бот для сбора средств.\n\n"
        "*Команды администратора:*\n"
        "/setfamilies — задать список фамилий\n"
        "/families — показать список фамилий\n"
        "/new <описание> [сумма] — новый сбор. Пример: `/new Подарок 300`\n"
        "/all — все сборы в этом чате\n"
        "/check <Фамилия> — где числится фамилия\n"
        "/unpay <Фамилия> [#N] — вернуть в должники\n"
        "/delcollection #N — удалить сбор по номеру\n"
        "/clearall — удалить все сборы в этом чате\n"
        "/setuser <Фамилия> [@username] — привязать Telegram\n"
        "/unsetuser <Фамилия> — удалить привязку\n"
        "/users — список привязок\n"
        "/bindold — привязать старые сборы к этому чату (одноразово)\n"
        "/export_db — скачать файл базы данных (резервная копия)\n"
        "/restart — перезапустить бота\n\n"
        "*Для участников:*\n"
        "Напишите «Фамилия скинул» или «Фамилия +» — отмечу оплату.\n"
        "Можно указать прошлый сбор: «Фамилия + #2».\n\n"
        "«Список должников» — все сборы с итогами.\n"
        "«Список должников #2» — только по сбору №2."
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
    chat_id = update.effective_chat.id
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

    set_families(chat_id, unique)
    await update.message.reply_text(
        f"✅ Сохранено {len(unique)} фамилий для этого чата:\n" + ", ".join(unique)
    )


async def cmd_families(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    fams = get_families(chat_id)
    if not fams:
        await update.message.reply_text("Список пуст. Задайте через /setfamilies")
        return
    await update.message.reply_text("👥 Список фамилий этого чата:\n" + ", ".join(fams))


async def start_new_collection(update: Update, title: str, amount: int = 0):
    chat_id = update.effective_chat.id
    families = get_families(chat_id)
    if not families:
        await update.message.reply_text(
            "⚠️ Сначала задайте список фамилий: /setfamilies"
        )
        return
    cid = create_collection(chat_id, title, families, amount)
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
    """Общий список: все сборы + итоги по людям."""
    chat_id = update.effective_chat.id
    debtors = get_all_debtors(chat_id)
    if not debtors:
        await update.message.reply_text("🎉 Должников нет — все сдали!")
        return

    by_collection = {}
    totals = {}
    order = []
    for cid, title, amount, family in debtors:
        key = (cid, title, amount)
        if key not in by_collection:
            by_collection[key] = []
            order.append(key)
        by_collection[key].append(family)
        totals[family] = totals.get(family, 0) + (amount or 0)

    lines = ["💸 *Список должников*\n"]
    for key in order:
        cid, title, amount = key
        fams = by_collection[key]
        members = get_members(cid)
        total_cnt = len(members)
        paid_cnt = sum(1 for _, p in members if p)
        amount_str = f"по {amount} ₽" if amount else "сумма не указана"
        lines.append(
            f"📌 *#{cid} «{title}»* _{amount_str}_ — сдало *{paid_cnt}/{total_cnt}*"
        )
        for f in fams:
            mention = format_mention(f)
            money = f"{amount} ₽" if amount else "—"
            if mention.startswith("@"):
                lines.append(f"   • {mention} ({f}) — {money}")
            elif mention.startswith("["):
                lines.append(f"   • {mention} — {money}")
            else:
                lines.append(f"   • {f} — {money}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━")
    lines.append("📊 *Итого по людям (по всем сборам этого чата):*")
    for f, total in sorted(totals.items(), key=lambda x: (-x[1], x[0])):
        mention = format_mention(f)
        money = f"{total} ₽" if total else "—"
        if mention.startswith("@"):
            lines.append(f"• {mention} ({f}) — *{money}*")
        elif mention.startswith("["):
            lines.append(f"• {mention} — *{money}*")
        else:
            lines.append(f"• {f} — *{money}*")

    text = "\n".join(lines)
    for part in split_long(text):
        await update.message.reply_text(
            part, parse_mode="Markdown", disable_web_page_preview=True
        )


async def show_debtors_for_collection(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: int):
    """Должники по одному сбору."""
    chat_id = update.effective_chat.id
    col = get_collection(cid, chat_id)
    if not col:
        await update.message.reply_text(f"⚠️ Сбор #{cid} не найден в этом чате.")
        return
    _, title, amount = col
    members = get_members(cid)
    unpaid = [f for f, p in members if not p]
    paid = [f for f, p in members if p]

    header = f"💸 *Должники по сбору #{cid} «{title}»*"
    if amount:
        header += f"\n_Сумма: {amount} ₽ с человека_"
    header += f"\n_Сдало: *{len(paid)}/{len(members)}*_\n"

    if not unpaid:
        await update.message.reply_text(
            header + f"\n🎉 Все сдали! ({len(paid)}/{len(members)})",
            parse_mode="Markdown",
        )
        return

    lines = [header]
    total_debt = 0
    for i, f in enumerate(unpaid, 1):
        mention = format_mention(f)
        money = f"{amount} ₽" if amount else "—"
        if mention.startswith("@"):
            lines.append(f"{i}. {mention} ({f}) — {money}")
        elif mention.startswith("["):
            lines.append(f"{i}. {mention} — {money}")
        else:
            lines.append(f"{i}. {f} — {money}")
        if amount:
            total_debt += amount

    if amount:
        lines.append(f"\n💰 *Итого к сбору: {total_debt} ₽*")

    lines.append(f"\n✅ Сдало: {len(paid)}/{len(members)}")
    if paid:
        lines.append(f"Список сдавших: {', '.join(paid)}")

    text = "\n".join(lines)
    for part in split_long(text):
        await update.message.reply_text(
            part, parse_mode="Markdown", disable_web_page_preview=True
        )


async def cmd_debtors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    m = re.search(r"#?\s*(\d+)", text)
    if not m:
        await update.message.reply_text(
            "Использование: `/debtors #2`\n"
            "Номера сборов смотрите в `/all`.",
            parse_mode="Markdown",
        )
        return
    cid = int(m.group(1))
    await show_debtors_for_collection(update, context, cid)


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    chat_id = update.effective_chat.id
    cols = get_all_collections(chat_id)
    if not cols:
        await update.message.reply_text("Сборов в этом чате пока нет.")
        return

    blocks = []
    for col in cols:
        paid = [f for f, p in col["members"] if p]
        unpaid = [f for f, p in col["members"] if not p]
        status = "🟢 активный" if col["active"] else "⚪ завершён"
        amount_str = f" • {col['amount']} ₽ с чел." if col["amount"] else ""
        b = f"*#{col['id']} — {col['title']}*\n"
        b += f"_Создан: {col['created_at']} • {status}{amount_str}_\n"
        b += f"✅ Сдало ({len(paid)}/{len(col['members'])}): {', '.join(paid) or '—'}\n"
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
    chat_id = update.effective_chat.id
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
        WHERE c.chat_id = ?
        ORDER BY c.id
    """, (chat_id,)).fetchall()
    conn.close()

    found = []
    for cid, title, active, amount, fam, paid in rows:
        if query in fam.lower():
            status = "🟢 активный" if active else "⚪ завершён"
            mark = "✅ оплачено" if paid else f"❌ не оплачено ({amount} ₽)"
            found.append(f"• #{cid} «{title}» ({status}) — {fam}: {mark}")

    if not found:
        await update.message.reply_text(
            f"❌ Фамилия, содержащая «{query}», не найдена ни в одном сборе этого чата."
        )
        return

    fams = get_families(chat_id)
    fams_match = [f for f in fams if query in f.lower()]
    msg = "🔎 *Результаты поиска (этот чат):*\n\n" + "\n".join(found)
    if fams_match:
        msg += "\n\n📋 В списке фамилий: " + ", ".join(fams_match)
    else:
        msg += "\n\n📋 В списке фамилий совпадений нет."
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_unpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    chat_id = update.effective_chat.id
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
        active = get_active_collection(chat_id)
        if not active:
            await update.message.reply_text("Нет активного сбора.")
            return
        cid, title = active[0], active[1]
    else:
        col = get_collection(cid, chat_id)
        if not col:
            await update.message.reply_text(f"⚠️ Сбор #{cid} не найден в этом чате.")
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


async def cmd_delcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    m = re.search(r"#?\s*(\d+)", text)
    if not m:
        await update.message.reply_text(
            "Использование: `/delcollection #2`\n\n"
            "Номер сбора смотрите в `/all`.",
            parse_mode="Markdown",
        )
        return
    cid = int(m.group(1))
    res = delete_collection(cid, chat_id)
    if not res:
        await update.message.reply_text(f"⚠️ Сбор #{cid} не найден в этом чате.")
        return
    title, members_count = res
    await update.message.reply_text(
        f"🗑 Сбор *#{cid} «{title}»* удалён.\n"
        f"Вместе с ним удалено участников: {members_count}.\n\n"
        f"Остальные сборы не затронуты.",
        parse_mode="Markdown",
    )


async def cmd_clearall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    chat_id = update.effective_chat.id
    cols = get_all_collections(chat_id)
    if not cols:
        await update.message.reply_text("В этом чате нет сборов.")
        return

    _pending_clear[chat_id] = True
    listing = "\n".join(f"• #{c['id']} — {c['title']}" for c in cols)
    await update.message.reply_text(
        f"⚠️ *Удалить ВСЕ сборы в этом чате?*\n\n"
        f"{listing}\n\n"
        f"Будет удалено: *{len(cols)}* сбора/сборов.\n"
        f"Список фамилий (`/families`) останется.\n\n"
        f"Напишите *`да`* для подтверждения или что угодно другое для отмены.",
        parse_mode="Markdown",
    )


async def cmd_setuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def cmd_bindold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    chat_id = update.effective_chat.id
    col_n, fam_n = bind_old_data_to_chat(chat_id)
    await update.message.reply_text(
        f"📦 Привязано к этому чату:\n"
        f"• сборов: {col_n}\n"
        f"• фамилий: {fam_n}\n\n"
        f"Теперь они будут видны в этом чате и не попадут в другие."
    )


async def cmd_export_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    if not os.path.exists(DB_PATH):
        await update.message.reply_text("⚠️ Файл базы не найден на сервере.")
        return
    size = os.path.getsize(DB_PATH)
    await update.message.reply_text(
        f"📦 Отправляю файл базы данных...\n"
        f"Размер: {size} байт\n\n"
        f"Сохраните его — это резервная копия всех сборов, фамилий и привязок."
    )
    try:
        with open(DB_PATH, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="collections_backup.db",
                caption="💾 Резервная копия базы данных бота",
            )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка при отправке файла: {e}")


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
    chat_id = update.effective_chat.id

    # 0) Подтверждение удаления всех сборов
    if _pending_clear.get(chat_id):
        del _pending_clear[chat_id]
        if lower == "да":
            if not is_admin(update.effective_user.id):
                await update.message.reply_text("⛔ Только администратор.")
                return
            n = delete_all_collections(chat_id)
            await update.message.reply_text(
                f"🗑 Удалено сборов: *{n}*.\nСписок фамилий не тронут — `/families`.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("❌ Отменено. Ничего не удалено.")
        return

    # 1) Список должников #N — только по одному сбору
    m_dd = re.match(r"^(?:список\s+должников|должники|кто\s+не\s+сдал)\s*#?\s*(\d+)\s*$", lower)
    if m_dd:
        cid = int(m_dd.group(1))
        await show_debtors_for_collection(update, context, cid)
        return

    # 2) Список должников / сдавших — общий
    if lower in ("список должников", "должники", "кто не сдал", "список сдавших"):
        if lower == "список сдавших":
            active = get_active_collection(chat_id)
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

    # 3) Новый сбор текстом
    m = re.match(r"^(?:новый\s+)?сбор\s+(.+)$", text, re.IGNORECASE)
    if m:
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Только администратор может создавать сборы.")
            return
        title, amount = parse_new_collection_text(m.group(1).strip())
        await start_new_collection(update, title, amount)
        return

    # 4) «Фамилия скинул/оплатил/+»
    m = PAID_RE.match(text)
    if m:
        family = m.group(1)
        explicit_cid = int(m.group(3)) if m.group(3) else None

        user = update.effective_user
        if user:
            save_contact(family.strip(), user.username, user.id, user.full_name)

        if explicit_cid:
            col = get_collection(explicit_cid, chat_id)
            if not col:
                await update.message.reply_text(
                    f"⚠️ Сбор #{explicit_cid} не найден в этом чате."
                )
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

        active = get_active_collection(chat_id)
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

        past = find_unpaid_in_past(chat_id, family)
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
                f"⚠️ «{family}» не найден в прошлых сборах этого чата."
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
    app.add_handler(CommandHandler("debtors", cmd_debtors))
    app.add_handler(CommandHandler("unpay", cmd_unpay))
    app.add_handler(CommandHandler("delcollection", cmd_delcollection))
    app.add_handler(CommandHandler("clearall", cmd_clearall))
    app.add_handler(CommandHandler("setuser", cmd_setuser))
    app.add_handler(CommandHandler("unsetuser", cmd_unsetuser))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("bindold", cmd_bindold))
    app.add_handler(CommandHandler("export_db", cmd_export_db))
    app.add_handler(CommandHandler("restart", cmd_restart))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    print("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()