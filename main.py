import asyncio
import html
import json
import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.methods.base import TelegramMethod
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ───────────────────────── НАСТРОЙКИ ─────────────────────────
BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN"
SAVE_OWN_DELETED = False   # True — сохранять и твои удалённые сообщения тоже
DB_PATH = "bot.db"
DEFAULT_TZ = 3             # часовой пояс по умолчанию (UTC+3 = Москва), меняется командой .tz
NICK_MIN_INTERVAL = 60     # не чаще раза в N секунд менять имя
AFK_COOLDOWN = 600         # AFK-ответ одному и тому же чату не чаще раза в N секунд

# ───────────────────── ПРЕМИУМ ЭМОДЗИ (в плашке) ─────────────────────
E_HEART = '<tg-emoji emoji-id="5465566379191087259">❤️</tg-emoji>'
E_SHIELD = '<tg-emoji emoji-id="5465611957384031828">🛡</tg-emoji>'
E_GIFT = '<tg-emoji emoji-id="5465477889979888341">🎁</tg-emoji>'
E_CHECK = '<tg-emoji emoji-id="5465357678140239852">✅</tg-emoji>'

logging.basicConfig(level=logging.INFO)
router = Router()


# ───────── Методы Bot API, не зависящие от версии aiogram ─────────
class DeleteBusinessMessagesCompat(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "deleteBusinessMessages"

    business_connection_id: str
    message_ids: list[int]


class SetBusinessAccountNameCompat(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "setBusinessAccountName"

    business_connection_id: str
    first_name: str
    last_name: Optional[str] = None


async def delete_msgs(bot: Bot, cid: str, ids: list[int]):
    return await bot(DeleteBusinessMessagesCompat(business_connection_id=cid, message_ids=ids))


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
            CREATE TABLE IF NOT EXISTS kv(
                conn_id TEXT, key TEXT, value TEXT, PRIMARY KEY(conn_id, key));
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

    def kv_get(self, cid, key, default=None):
        r = self.c.execute("SELECT value FROM kv WHERE conn_id=? AND key=?", (cid, key)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, cid, key, value):
        self.c.execute("REPLACE INTO kv VALUES(?,?,?)", (cid, key, json.dumps(value, ensure_ascii=False)))
        self.c.commit()

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
COMMANDS_TEXT = (
    "<b>📋 КОМАНДЫ:</b>\n"
    "<b>.fmt bold / italic / mono / underline / strike / spoiler / quote / off — авто-оформление</b>\n"
    "<b>.mute / .unmute — мут собеседника в этом чате</b>\n"
    "<b>.afk причина — автоответ «Владелец сейчас AFK», .unafk — выключить</b>\n"
    "<b>.nick on / off — время в нике: Имя // [ 00:00 ]</b>\n"
    "<b>.tz +3 — часовой пояс для ника</b>\n"
    "<b>.wordle слово — игра «Угадайте слово» (без слова — случайное)</b>\n"
    "<b>.dox — шаблон, .hack — «взлом»</b>\n"
    "<b>.type текст — печатная машинка</b>\n"
    "<b>.ball вопрос — шар судьбы, .coin — монетка, .roll 100 — число, .love — совместимость</b>\n"
    "<b>.help — список команд</b>"
)


def instruction_text() -> str:
    return (
        f"<b>{E_SHIELD} ИНСТРУКЦИЯ ПО ПОДКЛЮЧЕНИЮ</b>\n\n"
        "<b>1️⃣ Нужен Telegram Premium (функция «Telegram для бизнеса»).</b>\n"
        "<b>2️⃣ Открой Настройки → Telegram для бизнеса → Чат-боты.</b>\n"
        "<b>3️⃣ Введи username этого бота и выбери, в каких чатах он работает.</b>\n"
        "<b>4️⃣ Выдай боту ВСЕ права: ответы, удаление сообщений, изменение имени.</b>\n"
        "<b>5️⃣ Сохрани. Бот пришлёт сообщение об успешном подключении.</b>\n"
        "<b>6️⃣ Пиши команды в любом личном чате — бот сработает от твоего имени.</b>\n\n"
        f"{COMMANDS_TEXT}\n\n"
        "<b>🗑 Удалённые сообщения собеседников приходят сюда, в личку бота.</b>\n\n"
        f"{E_HEART} {E_SHIELD} {E_GIFT} {E_CHECK}"
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


DOX_STAGES = [
    (0, 3, "🔎 Поиск по неполным данным"),
    (4, 6, "🥖 Ищу отца в хлебном отделе"),
    (7, 9, "🧩 Собираю в единое"),
]

HACK_FRAMES = [
    "🔍 Поиск цели...",
    "🔓 Взлом аккаунта...\n[██░░░░░░░░] 20%",
    "📡 Подбор пароля...\n[████░░░░░░] 40%",
    "🗂 Скачивание переписок...\n[██████░░░░] 60%",
    "💾 Копирование фото кота...\n[████████░░] 80%",
    "🚀 Отправка данных...\n[██████████] 100%",
    "✅ Взлом завершён!",
]

BALL_ANSWERS = [
    "Бесспорно", "Предрешено", "Никаких сомнений", "Определённо да", "Можешь быть уверен",
    "Мне кажется — да", "Вероятнее всего", "Хорошие перспективы", "Знаки говорят — да",
    "Пока не ясно, попробуй снова", "Спроси позже", "Лучше не рассказывать",
    "Сконцентрируйся и спроси опять", "Даже не думай", "Мой ответ — нет",
    "По моим данным — нет", "Перспективы не очень хорошие", "Весьма сомнительно",
]

WORDS = [
    "книга", "кошка", "ручка", "стена", "лодка", "дождь", "земля", "ветер", "огонь",
    "пламя", "сосна", "мышка", "птица", "ложка", "мороз", "арбуз", "танец", "шторм", "песок",
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
mute_warned = set()
afk_last = {}
games = {}  # (conn_id, chat_id) -> состояние игры


async def get_owner(bot: Bot, cid: str):
    row = db.get_conn(cid)
    if row:
        return row
    conn = await bot.get_business_connection(cid)
    db.save_conn(cid, conn.user.id, conn.user_chat_id)
    return conn.user.id, conn.user_chat_id


async def edit_msg(bot: Bot, cid: str, chat_id: int, msg_id: int, text: str, markup=None) -> bool:
    try:
        await bot.edit_message_text(
            text=text,
            business_connection_id=cid,
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=markup,
        )
        return True
    except TelegramAPIError as e:
        logging.warning("edit failed: %s", e)
        return False


async def edit(bot: Bot, m: Message, text: str, markup=None) -> bool:
    return await edit_msg(bot, m.business_connection_id, m.chat.id, m.message_id, text, markup)


async def flash(bot: Bot, m: Message, text: str, delay: float = 2.0):
    """Показать временное уведомление вместо команды и удалить его."""
    await edit(bot, m, text)
    await asyncio.sleep(delay)
    try:
        await delete_msgs(bot, m.business_connection_id, [m.message_id])
    except Exception:
        pass


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


# ───────────────────── ВРЕМЯ В НИКЕ ─────────────────────
NICK_RE = re.compile(r"\s*//\s*\[\s*\d{1,2}:\d{2}\s*\]\s*$")


def strip_nick(s: str) -> str:
    return NICK_RE.sub("", s or "").strip()


def nick_names(first: str, last: str, stamp: str):
    suffix = f" // [ {stamp} ]"
    if last:
        return first, last[: 64 - len(suffix)] + suffix
    return first[: 64 - len(suffix)] + suffix, ""


def now_stamp(cid: str) -> str:
    tz = db.kv_get(cid, "tz", DEFAULT_TZ)
    return datetime.now(timezone(timedelta(hours=tz))).strftime("%H:%M")


async def apply_nick(bot: Bot, cid: str, user_chat_id: int, n: dict, stamp: str) -> bool:
    n["ts"] = time.time()
    first, last = nick_names(n["first"], n["last"], stamp)
    try:
        await bot(SetBusinessAccountNameCompat(
            business_connection_id=cid, first_name=first, last_name=last))
        n["stamp"] = stamp
        db.kv_set(cid, "nick", n)
        return True
    except Exception as e:
        db.kv_set(cid, "nick", n)
        logging.warning("nick update failed: %s", e)
        key = (cid, "nick", str(e))
        if key not in mute_warned:
            mute_warned.add(key)
            await bot.send_message(
                user_chat_id,
                "<b>⚠️ Не удалось изменить имя аккаунта.</b>\n"
                "<b>Включи боту право «Изменять имя» в Настройки → Telegram для бизнеса → Чат-боты.</b>\n"
                f"<b>Ошибка:</b> <code>{html.escape(str(e))}</code>",
            )
        return False


async def touch_activity(bot: Bot, cid: str, user_chat_id: int):
    """Владелец что-то написал — обновляем время «последней активности» в нике."""
    n = db.kv_get(cid, "nick")
    if not n or not n.get("on"):
        return
    stamp = now_stamp(cid)
    if stamp == n.get("stamp") or time.time() - n.get("ts", 0) < NICK_MIN_INTERVAL:
        return
    await apply_nick(bot, cid, user_chat_id, n, stamp)


# ───────────────────────── ИГРА WORDLE ─────────────────────────
def norm(s: str) -> str:
    return s.lower().replace("ё", "е").strip()


def score(guess: str, word: str):
    res = ["⬛"] * len(word)
    pool = list(word)
    for i, (g, w) in enumerate(zip(guess, word)):
        if g == w:
            res[i] = "🟩"
            pool[i] = None
    for i, g in enumerate(guess):
        if res[i] == "🟩":
            continue
        if g in pool:
            res[i] = "🟨"
            pool[pool.index(g)] = None
    return res


def board_markup(game: dict) -> InlineKeyboardMarkup:
    n = len(game["word"])
    rows = []
    for r in range(6):
        if r < len(game["rows"]):
            g, res = game["rows"][r]
            rows.append([
                InlineKeyboardButton(text=f"{res[i]}{g[i].upper()}", callback_data="noop")
                for i in range(n)
            ])
        else:
            rows.append([InlineKeyboardButton(text="⬜", callback_data="noop") for _ in range(n)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_wordle(bot: Bot, m: Message, raw_arg: str):
    cid = m.business_connection_id
    word = norm(raw_arg) if raw_arg else random.choice(WORDS)
    if not re.fullmatch(r"[а-яa-z]{3,8}", word):
        await flash(bot, m, "<b>Слово: 3–8 букв, только буквы.</b>", 3)
        return
    game = {"word": word, "rows": [], "msg_id": m.message_id}
    games[(cid, m.chat.id)] = game
    title = "<b>Угадайте слово:</b>"
    if await edit(bot, m, title, board_markup(game)):
        return
    # запасной вариант: прячем слово и отправляем новое сообщение с кнопками
    await edit(bot, m, "🎮")
    try:
        sent = await bot.send_message(
            chat_id=m.chat.id, text=title, business_connection_id=cid,
            reply_markup=board_markup(game),
        )
        game["msg_id"] = sent.message_id
        await delete_msgs(bot, cid, [m.message_id])
    except Exception as e:
        logging.warning("wordle fallback failed: %s", e)


async def handle_guess(bot: Bot, m: Message) -> bool:
    cid = m.business_connection_id
    game = games.get((cid, m.chat.id))
    if not game or not m.text:
        return False
    guess = norm(m.text)
    word = game["word"]
    if len(guess) != len(word) or not guess.isalpha():
        return False
    game["rows"].append((guess, score(guess, word)))
    won = guess == word
    lost = not won and len(game["rows"]) >= 6
    text = "<b>Угадайте слово:</b>"
    if won:
        text = f"<b>✅ Угадано! Слово: {html.escape(word.upper())}</b>"
    elif lost:
        text = f"<b>❌ Не угадали. Слово: {html.escape(word.upper())}</b>"
    if won or lost:
        games.pop((cid, m.chat.id), None)
    await edit_msg(bot, cid, m.chat.id, game["msg_id"], text, board_markup(game))
    return True


@router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


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
            f"<b>{E_CHECK} Бот успешно подключён к аккаунту!</b>\n\n{COMMANDS_TEXT}",
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
        asyncio.create_task(touch_activity(bot, cid, user_chat_id))
        await handle_owner(m, bot)
        return

    # ── мут ──
    if db.is_muted(cid, m.chat.id):
        try:
            await delete_msgs(bot, cid, [m.message_id])
            logging.info("muted message %s deleted", m.message_id)
        except Exception as e:
            logging.warning("mute delete failed: %s", e)
            key = (cid, "mute", str(e))
            if key not in mute_warned:
                mute_warned.add(key)
                await bot.send_message(
                    user_chat_id,
                    "<b>⚠️ Мут не сработал: не удалось удалить сообщение собеседника.</b>\n"
                    "<b>Проверь права бота: «Удалять все сообщения» и «Удалять отправленные».</b>\n"
                    f"<b>Ошибка:</b> <code>{html.escape(str(e))}</code>",
                )
        return

    cache(m)

    # ── игра ──
    if await handle_guess(bot, m):
        return

    # ── AFK ──
    afk = db.kv_get(cid, "afk")
    if afk and afk.get("on"):
        key = (cid, m.chat.id)
        if time.time() - afk_last.get(key, 0) >= AFK_COOLDOWN:
            afk_last[key] = time.time()
            try:
                await bot.send_message(
                    chat_id=m.chat.id,
                    business_connection_id=cid,
                    text=f"<b>Владелец сейчас AFK. Причина: {html.escape(afk.get('reason') or 'не указана')}</b>",
                )
            except TelegramAPIError as e:
                logging.warning("afk reply failed: %s", e)


@router.edited_business_message()
async def on_edited(m: Message):
    db.update_text(m.business_connection_id, m.chat.id, m.message_id, m.text or m.caption or "")


async def handle_owner(m: Message, bot: Bot):
    cid = m.business_connection_id
    text = (m.text or "").strip()

    if text.startswith("."):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        raw_arg = parts[1].strip() if len(parts) > 1 else ""
        arg = raw_arg.lower()

        if cmd == ".dox":
            for start, end, title in DOX_STAGES:
                for n in range(start, end + 1):
                    bar = "█" * n + "░" * (10 - n)
                    await edit(bot, m, f"<b>{title}...</b>\n<b>[{bar}] {n * 10}%</b>")
                    await asyncio.sleep(0.7)
            await edit(bot, m, "<b>✅ Итог</b>\n<b>[██████████] 100%</b>")
            await asyncio.sleep(0.8)
            await edit(bot, m, dox_text(m.chat.first_name or m.chat.title or ""))
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

        if cmd == ".afk":
            db.kv_set(cid, "afk", {"on": True, "reason": raw_arg or "не указана"})
            afk_last.clear()
            await flash(bot, m, f"<b>💤 AFK включён. Причина: {html.escape(raw_arg or 'не указана')}</b>")
            return

        if cmd == ".unafk":
            db.kv_set(cid, "afk", {"on": False, "reason": ""})
            await flash(bot, m, "<b>✅ AFK выключен.</b>")
            return

        if cmd == ".tz":
            try:
                tz = int(arg.replace("+", ""))
                assert -12 <= tz <= 14
            except Exception:
                await flash(bot, m, "<b>Пример: .tz +3</b>")
                return
            db.kv_set(cid, "tz", tz)
            await flash(bot, m, f"<b>🕐 Часовой пояс: UTC{tz:+d}</b>")
            return

        if cmd == ".nick":
            _, user_chat_id = await get_owner(bot, cid)
            if arg == "on":
                conn = await bot.get_business_connection(cid)
                n = {
                    "on": True,
                    "first": strip_nick(conn.user.first_name) or conn.user.first_name,
                    "last": strip_nick(conn.user.last_name or ""),
                    "stamp": "", "ts": 0,
                }
                ok = await apply_nick(bot, cid, user_chat_id, n, now_stamp(cid))
                db.kv_set(cid, "nick", n)
                await flash(bot, m, "<b>🕐 Время в нике включено.</b>" if ok else "<b>⚠️ Не вышло, смотри личку бота.</b>", 3)
            elif arg == "off":
                n = db.kv_get(cid, "nick")
                if n:
                    n["on"] = False
                    db.kv_set(cid, "nick", n)
                    try:
                        await bot(SetBusinessAccountNameCompat(
                            business_connection_id=cid, first_name=n["first"], last_name=n["last"]))
                    except Exception as e:
                        logging.warning("nick restore failed: %s", e)
                await flash(bot, m, "<b>🕐 Время в нике выключено.</b>")
            else:
                await flash(bot, m, "<b>Пример: .nick on / .nick off</b>")
            return

        if cmd == ".wordle":
            await start_wordle(bot, m, raw_arg)
            return

        if cmd == ".type" and raw_arg:
            step = max(1, len(raw_arg) // 12)
            for i in range(step, len(raw_arg) + 1, step):
                await edit(bot, m, html.escape(raw_arg[:i]) + "▌")
                await asyncio.sleep(0.5)
            await edit(bot, m, html.escape(raw_arg))
            return

        if cmd == ".ball":
            await edit(bot, m, f"<b>🎱 {html.escape(raw_arg or 'Вопрос')}</b>\n{random.choice(BALL_ANSWERS)}")
            return

        if cmd == ".coin":
            await edit(bot, m, f"<b>🪙 {random.choice(['Орёл', 'Решка'])}</b>")
            return

        if cmd == ".roll":
            top = int(arg) if arg.isdigit() and int(arg) > 1 else 100
            await edit(bot, m, f"<b>🎲 {random.randint(1, top)}</b> из {top}")
            return

        if cmd == ".love":
            pct = random.randint(1, 100)
            name = html.escape(m.chat.first_name or "тобой")
            for n in range(0, pct + 1, max(1, pct // 8)):
                bar = "❤️" * (n // 10) + "🤍" * (10 - n // 10)
                await edit(bot, m, f"<b>Ты + {name}</b>\n{bar} {n}%")
                await asyncio.sleep(0.6)
            await edit(bot, m, f"<b>Ты + {name}</b>\n{'❤️' * (pct // 10)}{'🤍' * (10 - pct // 10)} <b>{pct}%</b>")
            return

        if cmd == ".fmt":
            mode = FMT_ALIASES.get(arg, arg)
            if mode in FMT_TAGS or mode == "off":
                db.kv_set(cid, "fmt", mode)
                await flash(bot, m, f"<b>✨ Авто-оформление: {html.escape(mode)}</b>")
            else:
                await flash(bot, m, "<b>Доступно: bold, italic, mono, underline, strike, spoiler, quote, off</b>", 3)
            return

        if cmd == ".help":
            await edit(bot, m, COMMANDS_TEXT)
            return
        # неизвестная команда — обрабатываем как обычный текст

    cache(m)

    # авто-оформление
    mode = db.kv_get(cid, "fmt", "off")
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
