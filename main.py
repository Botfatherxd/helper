import asyncio
import html
import logging
import sqlite3

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

# ───────────────────────── НАСТРОЙКИ ─────────────────────────
BOT_TOKEN = "9034580639:AAEd-ReBIIRMqbqklCeXT1VslOcak_lY2Ss"
SAVE_OWN_DELETED = False  # True — сохранять и твои удалённые сообщения тоже
DB_PATH = "bot.db"

# ───────────────────── ПРЕМИУМ ЭМОДЗИ (в плашке) ─────────────────────
E_HEART = '<tg-emoji emoji-id="5465566379191087259">❤️</tg-emoji>'
E_SHIELD = '<tg-emoji emoji-id="5465611957384031828">🛡</tg-emoji>'
E_GIFT = '<tg-emoji emoji-id="5465477889979888341">🎁</tg-emoji>'
E_CHECK = '<tg-emoji emoji-id="5465357678140239852">✅</tg-emoji>'

logging.basicConfig(level=logging.INFO)
router = Router()


# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
class DB:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.executescript(
            """
            CREATE TABLE IF NOT EXISTS connections(
                id TEXT PRIMARY KEY, owner_id INTEGER, user_chat_id INTEGER);
            CREATE TABLE IF NOT EXISTS muted(
                conn_id TEXT, chat_id INTEGER, PRIMARY KEY(conn_id, chat_id));
            CREATE TABLE IF NOT EXISTS settings(
                conn_id TEXT PRIMARY KEY, fmt TEXT DEFAULT 'off');
            CREATE TABLE IF NOT EXISTS messages(
                conn_id TEXT, chat_id INTEGER, msg_id INTEGER,
                from_id INTEGER, from_name TEXT, ctype TEXT,
                text TEXT, file_id TEXT,
                PRIMARY KEY(conn_id, chat_id, msg_id));
            """
        )
        self.c.commit()

    def save_conn(self, cid, owner_id, user_chat_id):
        self.c.execute("REPLACE INTO connections VALUES(?,?,?)", (cid, owner_id, user_chat_id))
        self.c.commit()

    def get_conn(self, cid):
        return self.c.execute(
            "SELECT owner_id, user_chat_id FROM connections WHERE id=?", (cid,)
        ).fetchone()

    def set_mute(self, cid, chat_id, on: bool):
        if on:
            self.c.execute("INSERT OR IGNORE INTO muted VALUES(?,?)", (cid, chat_id))
        else:
            self.c.execute("DELETE FROM muted WHERE conn_id=? AND chat_id=?", (cid, chat_id))
        self.c.commit()

    def is_muted(self, cid, chat_id) -> bool:
        return self.c.execute(
            "SELECT 1 FROM muted WHERE conn_id=? AND chat_id=?", (cid, chat_id)
        ).fetchone() is not None

    def set_fmt(self, cid, fmt):
        self.c.execute("REPLACE INTO settings VALUES(?,?)", (cid, fmt))
        self.c.commit()

    def get_fmt(self, cid) -> str:
        r = self.c.execute("SELECT fmt FROM settings WHERE conn_id=?", (cid,)).fetchone()
        return r[0] if r else "off"

    def save_msg(self, cid, chat_id, msg_id, from_id, from_name, ctype, text, file_id):
        self.c.execute(
            "REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?)",
            (cid, chat_id, msg_id, from_id, from_name, ctype, text, file_id),
        )
        self.c.commit()

    def update_text(self, cid, chat_id, msg_id, text):
        self.c.execute(
            "UPDATE messages SET text=? WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (text, cid, chat_id, msg_id),
        )
        self.c.commit()

    def get_msg(self, cid, chat_id, msg_id):
        return self.c.execute(
            "SELECT from_id, from_name, ctype, text, file_id FROM messages "
            "WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (cid, chat_id, msg_id),
        ).fetchone()


db = DB(DB_PATH)

# ───────────────────────── ТЕКСТЫ ─────────────────────────
def instruction_text() -> str:
    return (
        f"<b>{E_SHIELD} ИНСТРУКЦИЯ ПО ПОДКЛЮЧЕНИЮ</b>\n\n"
        "<b>1️⃣ Нужен Telegram Premium (функция «Telegram для бизнеса»).</b>\n"
        "<b>2️⃣ Открой Настройки → Telegram для бизнеса → Чат-боты.</b>\n"
        "<b>3️⃣ Введи username этого бота и выбери, в каких чатах он работает.</b>\n"
        "<b>4️⃣ Выдай боту права: отвечать на сообщения, удалять отправленные "
        "и ВСЕ сообщения (нужно для мута).</b>\n"
        "<b>5️⃣ Сохрани. Бот пришлёт сообщение о успешном подключении.</b>\n"
        "<b>6️⃣ Пиши команды в любом личном чате — бот сработает от твоего имени.</b>\n\n"
        "<b>📋 КОМАНДЫ:</b>\n"
        "<b>.fmt bold / italic / mono / underline / strike / spoiler / quote / off — "
        "авто-оформление твоих сообщений</b>\n"
        "<b>.mute — мут собеседника (его новые сообщения удаляются)</b>\n"
        "<b>.unmute — снять мут</b>\n"
        "<b>.dox — фановый шаблон</b>\n"
        "<b>.hack — анимация «взлома»</b>\n"
        "<b>.help — список команд</b>\n\n"
        "<b>🗑 Удалённые сообщения собеседников приходят сюда, в личку бота.</b>\n\n"
        f"{E_HEART} {E_SHIELD} {E_GIFT} {E_CHECK}"
    )


def help_text() -> str:
    return (
        "<b>📋 КОМАНДЫ</b>\n"
        "<b>.fmt bold|italic|mono|underline|strike|spoiler|quote|off</b>\n"
        "<b>.mute / .unmute</b>\n"
        "<b>.dox</b> · <b>.hack</b> · <b>.help</b>"
    )


def dox_text(name: str) -> str:
    name = html.escape(name or "Жертва")
    return (
        f"<b>🕵️ DOX: {name}</b>\n"
        "<blockquote>"
        "страна: расiя\n"
        "город: расiя\n"
        "улица: расiя\n"
        "дом: расiя\n"
        "мама: расiя\n"
        "папа: расiя\n"
        "бабушка: расiя\n"
        "кот: расiя\n"
        "ip: расiя.расiя.расiя.расiя\n"
        "пароль от wifi: расiя\n"
        "любимая еда: расiя\n"
        "паспорт: расiя\n"
        "</blockquote>"
    )


HACK_FRAMES = [
    "🔍 Поиск цели...",
    "🔓 Взлом аккаунта...\n[██░░░░░░░░] 20%",
    "📡 Подбор пароля...\n[████░░░░░░] 40%",
    "🗂 Скачивание переписок...\n[██████░░░░] 60%",
    "💾 Копирование фото кота...\n[████████░░] 80%",
    "🚀 Отправка данных...\n[██████████] 100%",
    "✅ Взлом завершён!",
]

FMT_TAGS = {
    "bold": ("<b>", "</b>"),
    "italic": ("<i>", "</i>"),
    "mono": ("<code>", "</code>"),
    "underline": ("<u>", "</u>"),
    "strike": ("<s>", "</s>"),
    "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
    "quote": ("<blockquote>", "</blockquote>"),
}
FMT_ALIASES = {
    "жирный": "bold", "b": "bold",
    "курсив": "italic", "i": "italic",
    "моно": "mono", "code": "mono",
    "подчеркнутый": "underline", "u": "underline",
    "зачеркнутый": "strike", "s": "strike",
    "спойлер": "spoiler",
    "цитата": "quote",
    "выкл": "off", "off": "off",
}

# ───────────────────────── ХЕЛПЕРЫ ─────────────────────────
async def get_owner(bot: Bot, cid: str):
    row = db.get_conn(cid)
    if row:
        return row
    conn = await bot.get_business_connection(cid)
    db.save_conn(cid, conn.user.id, conn.user_chat_id)
    return conn.user.id, conn.user_chat_id


async def edit(bot: Bot, m: Message, text: str):
    try:
        await bot.edit_message_text(
            text=text,
            business_connection_id=m.business_connection_id,
            chat_id=m.chat.id,
            message_id=m.message_id,
        )
    except TelegramAPIError as e:
        logging.warning("edit failed: %s", e)


async def flash(bot: Bot, m: Message, text: str, delay: float = 2.0):
    """Показать временное уведомление вместо команды и удалить его."""
    await edit(bot, m, text)
    await asyncio.sleep(delay)
    try:
        await bot.delete_business_messages(m.business_connection_id, [m.message_id])
    except TelegramAPIError:
        pass


DOX_STAGES = [
    (0, 3, "🔎 Поиск по неполным данным"),
    (4, 6, "🥖 Ищу отца в хлебном отделе"),
    (7, 9, "🧩 Собираю в единое"),
]
mute_warned = set()


async def run_dox(bot: Bot, m: Message):
    """Анимация: шкала заполняется понемногу, в конце итог."""
    for start, end, title in DOX_STAGES:
        for n in range(start, end + 1):
            bar = "█" * n + "░" * (10 - n)
            await edit(bot, m, f"<b>{title}...</b>\n<b>[{bar}] {n * 10}%</b>")
            await asyncio.sleep(0.7)
    await edit(bot, m, "<b>✅ Итог</b>\n<b>[██████████] 100%</b>")
    await asyncio.sleep(0.8)
    await edit(bot, m, dox_text(m.chat.first_name or m.chat.title or ""))


def extract(m: Message):
    if m.text:
        return "text", None
    if m.photo:
        return "photo", m.photo[-1].file_id
    for attr in ("animation", "video", "document", "audio", "voice", "video_note", "sticker"):
        obj = getattr(m, attr, None)
        if obj:
            return attr, obj.file_id
    return "other", None


def cache(m: Message):
    ctype, file_id = extract(m)
    u = m.from_user
    name = (u.full_name if u else "Неизвестно") + (f" (@{u.username})" if u and u.username else "")
    db.save_msg(
        m.business_connection_id, m.chat.id, m.message_id,
        u.id if u else 0, name, ctype, m.text or m.caption or "", file_id,
    )


# ───────────────────────── /start ─────────────────────────
@router.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(instruction_text())


# ───────────────────────── ПОДКЛЮЧЕНИЕ ─────────────────────────
@router.business_connection()
async def on_connection(conn: BusinessConnection, bot: Bot):
    if conn.is_enabled:
        db.save_conn(conn.id, conn.user.id, conn.user_chat_id)
        await bot.send_message(
            conn.user_chat_id,
            f"<b>{E_CHECK} Бот успешно подключён к аккаунту!</b>\n\n" + help_text(),
        )
    else:
        await bot.send_message(conn.user_chat_id, "<b>❌ Бот отключён от аккаунта.</b>")


# ───────────────────────── СООБЩЕНИЯ ─────────────────────────
@router.business_message()
async def on_business_message(m: Message, bot: Bot):
    cid = m.business_connection_id
    owner_id, user_chat_id = await get_owner(bot, cid)

    # сообщение от владельца аккаунта
    if m.from_user and m.from_user.id == owner_id:
        await handle_owner(m, bot)
        return

    # сообщение собеседника
    if db.is_muted(cid, m.chat.id):
        try:
            await bot.delete_business_messages(cid, [m.message_id])
        except TelegramAPIError as e:
            logging.warning("mute delete failed: %s", e)
            if cid not in mute_warned:
                mute_warned.add(cid)
                await bot.send_message(
                    user_chat_id,
                    "<b>⚠️ Мут не сработал: не удалось удалить сообщение собеседника.</b>\n"
                    "<b>Открой Настройки → Telegram для бизнеса → Чат-боты → твой бот "
                    "и включи право «Удалять все сообщения» (и «Удалять отправленные»).</b>\n"
                    f"<b>Ошибка:</b> <code>{html.escape(str(e))}</code>",
                )
        return

    cache(m)


@router.edited_business_message()
async def on_edited(m: Message):
    db.update_text(m.business_connection_id, m.chat.id, m.message_id, m.text or m.caption or "")


async def handle_owner(m: Message, bot: Bot):
    cid = m.business_connection_id
    text = (m.text or "").strip()

    if text.startswith("."):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        if cmd == ".dox":
            await run_dox(bot, m)
            return

        if cmd == ".hack":
            for frame in HACK_FRAMES:
                await edit(bot, m, f"<b>{frame}</b>")
                await asyncio.sleep(0.9)
            return

        if cmd == ".mute":
            db.set_mute(cid, m.chat.id, True)
            await flash(bot, m, "<b>🔇 Собеседник замучен — его сообщения будут удаляться.</b>")
            return

        if cmd == ".unmute":
            db.set_mute(cid, m.chat.id, False)
            await flash(bot, m, "<b>🔊 Мут снят.</b>")
            return

        if cmd == ".fmt":
            mode = FMT_ALIASES.get(arg, arg)
            if mode in FMT_TAGS or mode == "off":
                db.set_fmt(cid, mode)
                await flash(bot, m, f"<b>✨ Авто-оформление: {html.escape(mode)}</b>")
            else:
                await flash(bot, m, "<b>Доступно: bold, italic, mono, underline, strike, spoiler, quote, off</b>", 3)
            return

        if cmd == ".help":
            await edit(bot, m, help_text())
            return
        # неизвестная команда — обрабатываем как обычный текст

    cache(m)

    # авто-оформление
    mode = db.get_fmt(cid)
    if mode != "off" and m.text:
        open_t, close_t = FMT_TAGS[mode]
        body = html.escape(m.text) if mode == "mono" else m.html_text
        await edit(bot, m, f"{open_t}{body}{close_t}")


# ───────────────────── УДАЛЁННЫЕ СООБЩЕНИЯ ─────────────────────
SEND_METHODS = {
    "photo": "send_photo", "video": "send_video", "animation": "send_animation",
    "document": "send_document", "audio": "send_audio", "voice": "send_voice",
    "video_note": "send_video_note", "sticker": "send_sticker",
}


@router.deleted_business_messages()
async def on_deleted(ev: BusinessMessagesDeleted, bot: Bot):
    cid = ev.business_connection_id
    owner_id, user_chat_id = await get_owner(bot, cid)

    for mid in ev.message_ids:
        row = db.get_msg(cid, ev.chat.id, mid)
        if not row:
            continue
        from_id, from_name, ctype, text, file_id = row
        if from_id == owner_id and not SAVE_OWN_DELETED:
            continue

        header = (
            f"<b>🗑 Удалённое сообщение</b>\n"
            f"<b>Чат:</b> {html.escape(ev.chat.full_name if hasattr(ev.chat, 'full_name') else str(ev.chat.id))}\n"
            f"<b>От:</b> {html.escape(from_name)}"
        )
        try:
            if ctype == "text" or ctype == "other" or not file_id:
                await bot.send_message(user_chat_id, f"{header}\n\n{html.escape(text or '[не удалось сохранить]')}")
            else:
                await bot.send_message(user_chat_id, header)
                method = getattr(bot, SEND_METHODS[ctype])
                kwargs = {ctype: file_id}
                if ctype not in ("sticker", "video_note") and text:
                    kwargs.update(caption=text[:1024], parse_mode=None)
                await method(user_chat_id, **kwargs)
        except (TelegramAPIError, KeyError) as e:
            logging.warning("send deleted failed: %s", e)


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
