import sqlite3
import logging
import re
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.getenv("BOT_TOKEN")  # ← ЗАМЕНИТЕ на новый после revoke
ADMIN_IDS = [1920218354]   # сюда впишите ваш user_id (узнать: /id). Пусто = любой админ.
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
        active INTEGER DEFAULT 1
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


def create_collection(title, families):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE collections SET active=0 WHERE active=1")
    c.execute("INSERT INTO collections (title, active) VALUES (?, 1)", (title,))
    cid = c.lastrowid
    for f in families:
        c.execute("INSERT INTO members (collection_id, family) VALUES (?, ?)", (cid, f))
    conn.commit()
    conn.close()
    return cid


def get_active_collection():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, title FROM collections WHERE active=1 ORDER BY id DESC LIMIT 1"
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
    """Возвращает ('ok', имя) | ('already', имя) | ('not_found', None).
    Регистронезависимо для любого Юникода (включая кириллицу)."""
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


def get_collection(cid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT id, title FROM collections WHERE id=?", (cid,)).fetchone()
    conn.close()
    return row


def find_unpaid_in_past(family):
    """Ищет неуплаченные записи с этой фамилией во всех НЕактивных сборах.
    Возвращает список (collection_id, title, member_id, family)."""
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


def mark_paid(cid, family):
    """Возвращает ('ok', имя) | ('already', имя) | ('not_found', None).
    Регистронезависимо для любого Юникода (включая кириллицу)."""
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


def get_collection(cid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT id, title FROM collections WHERE id=?", (cid,)).fetchone()
    conn.close()
    return row


def find_unpaid_in_past(family):
    """Ищет неуплаченные записи с этой фамилией во всех НЕактивных сборах.
    Возвращает список (collection_id, title, member_id, family)."""
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


def get_all_collections():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, title, created_at, active FROM collections ORDER BY id"
    ).fetchall()
    result = []
    for r in rows:
        members = c.execute(
            "SELECT family, paid FROM members WHERE collection_id=? ORDER BY family", (r[0],)
        ).fetchall()
        result.append(
            {"id": r[0], "title": r[1], "created_at": r[2], "active": r[3], "members": members}
        )
    conn.close()
    return result


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
        "/setfamilies — задать список фамилий (после команды, каждую с новой строки или через запятую)\n"
        "/families — показать текущий список фамилий\n"
        "/new <описание> — начать новый сбор (или напишите «Новый сбор <описание>»)\n"
        "/all — показать все сборы с описанием и статусами\n"
        "/check <Фамилия> — проверить, где числится фамилия\n"
        "/restart — перезапустить бота\n\n"
        "*Для участников:*\n"
        "Напишите «Фамилия скинул» (или «скинула»/«оплатил») — я отмечу оплату.\n\n"
        "«Список должников» — покажу, кто ещё не сдал."
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


async def start_new_collection(update: Update, title: str):
    families = get_families()
    if not families:
        await update.message.reply_text(
            "⚠️ Сначала задайте список фамилий: /setfamilies"
        )
        return
    cid = create_collection(title, families)
    listing = "\n".join(f"{i+1}. {f}" for i, f in enumerate(families))
    await update.message.reply_text(
        f"📢 *Новый сбор #{cid}:* {title}\n\n👥 Кто участвует:\n{listing}\n\n"
        f"Когда сдадите — напишите «Фамилия скинул».",
        parse_mode="Markdown",
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор может создавать сборы.")
        return
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Укажите описание: /new Сбор по 300 руб на подарок")
        return
    await start_new_collection(update, parts[1].strip())


async def show_debtors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = get_active_collection()
    if not active:
        await update.message.reply_text("Нет активного сбора.")
        return
    cid, title = active
    members = get_members(cid)
    unpaid = [f for f, p in members if not p]
    paid = [f for f, p in members if p]

    if not unpaid:
        await update.message.reply_text(
            f"🎉 По сбору «{title}» сдали все! ({len(paid)}/{len(members)})"
        )
        return

    listing = "\n".join(f"{i+1}. {f}" for i, f in enumerate(unpaid))
    await update.message.reply_text(
        f"💸 *Должники по сбору «{title}»*\n\n{listing}\n\n"
        f"Сдали: {len(paid)}/{len(members)}",
        parse_mode="Markdown",
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
        b = f"*#{col['id']} — {col['title']}*\n"
        b += f"_Создан: {col['created_at']} • {status}_\n"
        b += f"✅ Сдали ({len(paid)}/{len(col['members'])}): {', '.join(paid) or '—'}\n"
        if unpaid:
            b += f"❌ Должники ({len(unpaid)}): {', '.join(unpaid)}"
        blocks.append(b)

    text = "\n\n".join(blocks)
    for part in split_long(text):
        await update.message.reply_text(part, parse_mode="Markdown")


# ---------- Обработка обычных сообщений ----------
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только администратор.")
        return
    await update.message.reply_text("🔄 Перезапускаю бота... Через 5–10 секунд снова будет в строю.")
    import sys
    logging.info("Bot restart requested by admin")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)   # BotHost автоматически поднимет процесс заново


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
        SELECT c.id, c.title, c.active, m.family, m.paid
        FROM members m
        JOIN collections c ON c.id = m.collection_id
        ORDER BY c.id
    """).fetchall()
    conn.close()

    found = []
    for cid, title, active, fam, paid in rows:
        if query in fam.lower():   # частичное совпадение
            status = "🟢 активный" if active else "⚪ завершён"
            mark = "✅ оплачено" if paid else "❌ не оплачено"
            found.append(f"• #{cid} «{title}» ({status}) — {fam}: {mark}")

    if not found:
        await update.message.reply_text(
            f"❌ Фамилия, содержащая «{query}», не найдена ни в одном сборе."
        )
        return

    # Текущий список фамилий
    fams = get_families()
    fams_match = [f for f in fams if query in f.lower()]
    msg = "🔎 *Результаты поиска:*\n\n" + "\n".join(found)
    if fams_match:
        msg += "\n\n📋 В общем списке фамилий: " + ", ".join(fams_match)
    else:
        msg += "\n\n📋 В общем списке фамилий совпадений нет (только в сборах)."
    await update.message.reply_text(msg, parse_mode="Markdown")

    # 1) Список должников / сдавших
    if lower in ("список должников", "должники", "кто не сдал", "список сдавших"):
        if lower == "список сдавших":
            active = get_active_collection()
            if not active:
                await update.message.reply_text("Нет активного сбора.")
                return
            cid, title = active
            paid = [f for f, p in get_members(cid) if p]
            await update.message.reply_text(
                f"✅ По «{title}» сдали:\n" + ("\n".join(f"• {f}" for f in paid) or "—"),
            )
            return
        await show_debtors(update, context)
        return

    # 2) Новый сбор текстом: «Новый сбор ...» или «Сбор ...»
    m = re.match(r"^(?:новый\s+)?сбор\s+(.+)$", text, re.IGNORECASE)
    if m:
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Только администратор может создавать сборы.")
            return
        await start_new_collection(update, m.group(1).strip())
        return

    # 3) «Фамилия скинул/скинула/оплатил...» (+ опционально «в сборе N» или «#N»)
    m = PAID_RE.match(text)
    if m:
        family = m.group(1)
        explicit_cid = int(m.group(3)) if m.group(3) else None

        # --- Если явно указан сбор ---
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

        # --- Иначе: сначала активный сбор ---
        active = get_active_collection()
        if active:
            cid, title = active
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

        # --- Ищем в прошлых сборах ---
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

        # --- Ничего не найдено ---
        if active:
            await update.message.reply_text(
                f"⚠️ «{family}» не найден в текущем сборе «{active[1]}» и в прошлых сборах."
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
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("check", cmd_check))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    print("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()