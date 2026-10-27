"""
Telegram-бот «Получить промт» (aiogram 3.x)

- Админ создаёт публикацию: текст/фото для канала + СВОЙ промт (в канале его нет)
  + выбирает, на какие каналы/ботов нужно подписаться именно для этой публикации.
- Бот публикует пост в канал с кнопкой «Получить промт» (deep-link ?start=p<ID>).
- Пользователь нажимает кнопку -> бот определяет публикацию, проверяет подписки этой публикации
  и присылает «Ваш промт 💛» + промт текстом (без фото) в <code>-блоке: нажатие копирует.
  Если промт длиннее PROMPT_FILE_THRESHOLD символов — присылает его текстовым файлом .txt.

Переменные окружения: BOT_TOKEN, ADMIN_IDS, DB_PATH (см. README.md)
"""

import asyncio
import base64
import hashlib
import html
import json
import math
import logging
import os
import sqlite3
import tempfile
import time
from urllib.parse import quote

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("promt-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "bot.db")
# промт длиннее этого числа символов отправляется текстовым файлом (.txt), а не сообщением
FILE_THRESHOLD = int(os.getenv("PROMPT_FILE_THRESHOLD", "1500"))

# ── Генерации: OpenAI, оплата, рефералы ──
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-5.4-mini")  # анализ картинки -> промт
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")  # генерация фото
OPENAI_IMAGE_SIZE = os.getenv("OPENAI_IMAGE_SIZE", "1024x1536")
OPENAI_IMAGE_QUALITY = os.getenv("OPENAI_IMAGE_QUALITY", "medium")
OPENAI_INPUT_FIDELITY = os.getenv("OPENAI_INPUT_FIDELITY", "")  # "high" только для gpt-image-1/1.5
PROMPT_LANGUAGE = os.getenv("PROMPT_LANGUAGE", "Russian")
PAY_PHONE = os.getenv("PAY_PHONE", "+992 978117811")
PAY_METHODS = os.getenv("PAY_METHODS", "DCnext / Alif Mobi")
REF_FRIENDS_NEEDED = int(os.getenv("REF_FRIENDS_NEEDED", "3"))  # сколько друзей = бонус
REF_MAX_BONUSES = int(os.getenv("REF_MAX_BONUSES", "1"))  # сколько раз можно получить бонус
PACKAGES = [(1, 6.0), (3, 14.99), (10, 29.99)]  # (кол-во фото, цена в сомони)

# ── Хранилище в GitHub (необязательно): бэкап базы в приватный репозиторий ──
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # owner/name
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "data")  # отдельная ветка: пуши не трогают код в main
GITHUB_BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "main")  # от неё создаётся ветка данных
DB_ENCRYPT_KEY = os.getenv("DB_ENCRYPT_KEY", "")  # секретная фраза: база шифруется перед загрузкой
GITHUB_DB_FILE = os.getenv("GITHUB_DB_FILE", "bot.db.enc" if DB_ENCRYPT_KEY else "bot.db")
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL", "300"))  # секунд между проверками
GITHUB_API = "https://api.github.com"
GH_ENABLED = bool(GITHUB_TOKEN and GITHUB_REPO)
_last_backup_hash = ""

BOT_USERNAME = ""  # заполняется при старте

router = Router()
ADMIN = F.from_user.id.in_(ADMIN_IDS)


# ───────────────────────────── База данных ─────────────────────────────

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen INTEGER
            );
            CREATE TABLE IF NOT EXISTS posts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                photo TEXT,
                prompt TEXT NOT NULL,
                channel_id TEXT,
                message_id INTEGER,
                created INTEGER
            );
            -- каталог каналов и ботов, которые админ может требовать
            CREATE TABLE IF NOT EXISTS subs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,          -- 'channel' | 'bot'
                chat_id INTEGER,
                title TEXT,
                link TEXT NOT NULL
            );
            -- какие подписки нужны для конкретной публикации
            CREATE TABLE IF NOT EXISTS post_subs(
                post_id INTEGER,
                sub_id INTEGER,
                PRIMARY KEY(post_id, sub_id)
            );
            -- созданные промты (генерации); paid=1 — можно сгенерировать фото с лицом пользователя
            CREATE TABLE IF NOT EXISTS gens(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                ref_file TEXT,
                ref_mime TEXT,
                prompt TEXT,
                paid INTEGER,
                photo_used INTEGER DEFAULT 0,
                created INTEGER
            );
            CREATE TABLE IF NOT EXISTS payments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                qty INTEGER,
                amount REAL,
                receipt TEXT,
                receipt_kind TEXT,
                status TEXT DEFAULT 'pending',
                created INTEGER
            );
            CREATE TABLE IF NOT EXISTS claims(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                post_id INTEGER,
                ts INTEGER
            );
            """
        )
        ucols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        for name, ddl in (
            ("ref_by", "INTEGER"),
            ("free_gens", "INTEGER DEFAULT 1"),  # новым (и уже существующим) — 1 бесплатная генерация
            ("paid_gens", "INTEGER DEFAULT 0"),
            ("ref_bonus", "INTEGER DEFAULT 0"),
        ):
            if name not in ucols:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        cols = [r[1] for r in c.execute("PRAGMA table_info(subs)")]
        if "token" not in cols:  # токен бота для настоящей проверки запуска (необязателен)
            c.execute("ALTER TABLE subs ADD COLUMN token TEXT")


# ───────────────────────────── Бэкап в GitHub ─────────────────────────────

def _gh_headers(raw: bool = False) -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_url() -> str:
    return f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_DB_FILE}"


_fernet = None


def _crypto():
    """Fernet из секретной фразы DB_ENCRYPT_KEY (или None, если шифрование выключено)."""
    global _fernet
    if not DB_ENCRYPT_KEY:
        return None
    if _fernet is None:
        from cryptography.fernet import Fernet

        key = hashlib.pbkdf2_hmac("sha256", DB_ENCRYPT_KEY.encode(), b"prompt-bot-db", 200_000)
        _fernet = Fernet(base64.urlsafe_b64encode(key))
    return _fernet


async def _repo_is_private(http) -> bool:
    async with http.get(f"{GITHUB_API}/repos/{GITHUB_REPO}", headers=_gh_headers()) as r:
        r.raise_for_status()
        return bool((await r.json()).get("private"))


async def _ensure_branch(http) -> None:
    """Создаёт ветку данных от основной, если её ещё нет."""
    base = f"{GITHUB_API}/repos/{GITHUB_REPO}/git"
    async with http.get(f"{base}/ref/heads/{GITHUB_BRANCH}", headers=_gh_headers()) as r:
        if r.status == 200:
            return
        if r.status != 404:
            r.raise_for_status()
    async with http.get(f"{base}/ref/heads/{GITHUB_BASE_BRANCH}", headers=_gh_headers()) as r:
        r.raise_for_status()
        sha = (await r.json())["object"]["sha"]
    async with http.post(
        f"{base}/refs", json={"ref": f"refs/heads/{GITHUB_BRANCH}", "sha": sha}, headers=_gh_headers()
    ) as r:
        r.raise_for_status()
    log.info("Создана ветка %s для данных", GITHUB_BRANCH)


async def gh_restore() -> None:
    """Если локальной базы нет — скачать её из GitHub."""
    global _last_backup_hash
    if not GH_ENABLED or os.path.exists(DB_PATH):
        return
    import aiohttp

    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(_gh_url(), params={"ref": GITHUB_BRANCH}, headers=_gh_headers(raw=True)) as r:
                if r.status == 404:
                    log.info("В GitHub бэкапа пока нет — начинаем с чистой базы")
                    return
                r.raise_for_status()
                data = await r.read()
        f = _crypto()
        if f:
            data = f.decrypt(data)
        folder = os.path.dirname(DB_PATH)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(DB_PATH, "wb") as f:
            f.write(data)
        _last_backup_hash = hashlib.sha256(data).hexdigest()
        log.info("База восстановлена из GitHub (%d байт)", len(data))
    except Exception as e:
        log.error("Не удалось восстановить базу из GitHub: %s", e)


def _snapshot() -> bytes:
    """Целостный снимок базы (безопасно, даже если бот сейчас пишет в неё)."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.remove(tmp)


async def gh_backup(force: bool = False) -> str:
    """Загружает базу в GitHub, если она изменилась. Возвращает текст-статус."""
    global _last_backup_hash
    if not GH_ENABLED:
        return "GitHub-бэкап не настроен (нет GITHUB_TOKEN / GITHUB_REPO)."
    import aiohttp

    try:
        data = await asyncio.to_thread(_snapshot)
        digest = hashlib.sha256(data).hexdigest()
        if digest == _last_backup_hash and not force:
            return "Изменений нет."
        async with aiohttp.ClientSession() as http:
            if not DB_ENCRYPT_KEY and not await _repo_is_private(http):
                return ("❌ Репозиторий публичный, а DB_ENCRYPT_KEY не задан — "
                        "загружать базу открыто нельзя. Задайте DB_ENCRYPT_KEY.")
            await _ensure_branch(http)
            f = _crypto()
            payload = f.encrypt(data) if f else data
            sha = None
            async with http.get(_gh_url(), params={"ref": GITHUB_BRANCH}, headers=_gh_headers()) as r:
                if r.status == 200:
                    sha = (await r.json()).get("sha")
                elif r.status != 404:
                    r.raise_for_status()
            body = {
                "message": f"backup {time.strftime('%Y-%m-%d %H:%M:%S')}",
                "content": base64.b64encode(payload).decode(),
                "branch": GITHUB_BRANCH,
            }
            if sha:
                body["sha"] = sha
            async with http.put(_gh_url(), json=body, headers=_gh_headers()) as r:
                if r.status >= 400:
                    raise RuntimeError(f"{r.status}: {(await r.text())[:200]}")
        _last_backup_hash = digest
        log.info("Бэкап в GitHub загружен (%d байт)", len(data))
        return f"✅ Бэкап загружен в GitHub ({len(data) // 1024 + 1} КБ)."
    except Exception as e:
        log.error("Ошибка бэкапа в GitHub: %s", e)
        return f"❌ Ошибка бэкапа: {e}"


async def backup_loop() -> None:
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        await gh_backup()


# ───────────────────────────── Состояния ─────────────────────────────

class NewPost(StatesGroup):
    content = State()
    prompt = State()
    subs = State()
    channel = State()


class Create(StatesGroup):
    ref = State()   # ждём картинку-образец
    face = State()  # ждём фото лица для генерации


class Pay(StatesGroup):
    receipt = State()  # ждём чек


class AddChannel(StatesGroup):
    wait = State()


class AddBot(StatesGroup):
    wait = State()
    token = State()


# ───────────────────────────── Вспомогательное ─────────────────────────────

def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новая публикация", callback_data="adm:newpost")
    kb.button(text="📋 Публикации", callback_data="adm:posts")
    kb.button(text="📢 Каналы и боты для подписки", callback_data="adm:subs")
    kb.button(text="📊 Статистика", callback_data="adm:stats")
    kb.button(text="👤 Меню пользователя", callback_data="menu:home")
    kb.adjust(1)
    return kb.as_markup()


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu")]]
    )


def save_user(m: Message) -> bool:
    """Сохраняет пользователя, возвращает True, если он новый."""
    with db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_seen) VALUES(?,?,?)",
            (m.from_user.id, m.from_user.username, int(time.time())),
        )
        return cur.rowcount > 0


_err_notified: dict[int, float] = {}


async def notify_admins(bot: Bot, text: str) -> None:
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass


async def is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        cm = await bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        log.warning("get_chat_member(%s) failed: %s", chat_id, e)
        # бот не видит канал (удалён из админов / канал удалён) — предупредим админа, не чаще раза в час
        if time.time() - _err_notified.get(chat_id, 0) > 3600:
            _err_notified[chat_id] = time.time()
            await notify_admins(
                bot,
                f"⚠️ Не могу проверить подписку на канал <code>{chat_id}</code>: {html.escape(str(e))}\n"
                "Возможно, бота удалили из админов канала. Пока это не исправлено, "
                "пользователи не смогут получить промт.",
            )
        return False
    if cm.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        return False
    if cm.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(cm, "is_member", True))
    return True


# ───────────────────── Чистка чата (удаление прошлых сообщений) ─────────────────────

# служебные сообщения бота, которые нужно убрать при следующем действии: chat_id -> [message_id]
_trash: dict[int, list[int]] = {}
CLEAN_CMDS = {"/start", "/admin", "/cancel", "/backup", "/menu", "/give", "/aicheck"}


async def reply(m: Message, text: str, **kw) -> Message:
    """Ответ, который потом будет удалён функцией wipe()."""
    sent = await m.answer(text, **kw)
    _trash.setdefault(m.chat.id, []).append(sent.message_id)
    return sent


async def wipe(bot: Bot, chat_id: int) -> None:
    """Удаляет предыдущие служебные сообщения бота в этом чате."""
    for mid in _trash.pop(chat_id, []):
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


class CleanupMiddleware(BaseMiddleware):
    """После обработки удаляет команды и ответы админа в шагах мастера — чат остаётся чистым."""

    async def __call__(self, handler, event: Message, data: dict):
        result = await handler(event, data)
        cmd = (event.text or "").split(maxsplit=1)[0].split("@")[0] if event.text else ""
        is_admin = bool(event.from_user and event.from_user.id in ADMIN_IDS)
        if event.chat.type == "private" and ((data.get("raw_state") and is_admin) or cmd in CLEAN_CMDS):
            try:
                await event.delete()
            except Exception:
                pass
        return result


router.message.middleware(CleanupMiddleware())


def split_text(text: str, limit: int = 3500) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or [text]


def post_subs(post_id: int) -> list[sqlite3.Row]:
    with db() as c:
        return c.execute(
            "SELECT s.* FROM subs s JOIN post_subs ps ON ps.sub_id=s.id WHERE ps.post_id=? ORDER BY s.id",
            (post_id,),
        ).fetchall()


async def missing_channels(bot: Bot, user_id: int, post_id: int) -> list[sqlite3.Row]:
    res = []
    for s in post_subs(post_id):
        if s["kind"] == "channel" and not await is_member(bot, s["chat_id"], user_id):
            res.append(s)
    return res


_sub_bots: dict[str, Bot] = {}


def _sub_bot(token: str) -> Bot:
    """Клиент для бота из каталога (кэшируется, чтобы не создавать сессию на каждую проверку)."""
    if token not in _sub_bots:
        _sub_bots[token] = Bot(token)
    return _sub_bots[token]


async def bot_status(bot: Bot, sub: sqlite3.Row, user_id: int):
    """True — человек запускал этого бота, False — нет (или остановил/заблокировал),
    None — проверить нельзя (нет токена или ошибка), тогда работает кнопка «Готово»."""
    if not sub["token"]:
        return None
    try:
        await _sub_bot(sub["token"]).send_chat_action(user_id, "typing")
        return True
    except TelegramForbiddenError:
        return False  # бота остановили/заблокировали
    except TelegramBadRequest as e:
        if "chat not found" in str(e).lower():
            return False  # человек ещё не запускал бота
        log.warning("проверка бота %s: %s", sub["title"], e)
        return None
    except TelegramUnauthorizedError:
        key = -int(sub["id"])
        if time.time() - _err_notified.get(key, 0) > 3600:
            _err_notified[key] = time.time()
            await notify_admins(bot, f"⚠️ Токен бота {html.escape(sub['title'])} не работает (сменили или отозвали). "
                                     "Проверка запуска отключена, работает только кнопка «Готово». "
                                     "Обновите токен в «Каналы и боты».")
        return None
    except Exception as e:
        log.warning("проверка бота %s не удалась: %s", sub["title"], e)
        return None


async def send_prompt(bot: Bot, user_id: int, post_id: int, prompt: str) -> None:
    """Короткий промт — текстом в <code>-блоке, длинный — текстовым файлом."""
    if len(prompt) > FILE_THRESHOLD:
        # utf-8-sig: BOM помогает Блокноту/Windows правильно открыть кириллицу
        data = prompt.encode("utf-8-sig")
        await bot.send_document(
            user_id,
            BufferedInputFile(data, filename=f"prompt_{post_id}.txt"),
            caption="Ваш промт 💛\nОн длинный, поэтому прислан файлом — откройте и скопируйте текст.",
        )
        return
    await bot.send_message(user_id, "Ваш промт 💛")
    for chunk in split_text(prompt):
        await bot.send_message(user_id, f"<code>{html.escape(chunk)}</code>")


async def check_bots(bot: Bot, user_id: int, post_id: int):
    """Возвращает (боты, которых человек точно не запускал; боты без возможности проверки)."""
    not_started, manual = [], []
    for sub in post_subs(post_id):
        if sub["kind"] != "bot":
            continue
        st = await bot_status(bot, sub, user_id)
        if st is False:
            not_started.append(sub)
        elif st is None:
            manual.append(sub)
    return not_started, manual


async def process(bot: Bot, user_id: int, post_id: int, bots_confirmed: bool = False) -> None:
    """Проверяет подписки публикации и выдаёт её промт (или просит подписаться)."""
    await wipe(bot, user_id)  # убираем прошлые служебные сообщения (просьбу подписаться и т.п.)
    with db() as c:
        post = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        sent = await bot.send_message(user_id, "😔 Эта публикация не найдена или была удалена.")
        _trash.setdefault(user_id, []).append(sent.message_id)
        return

    missing = await missing_channels(bot, user_id, post_id)
    not_started, manual = await check_bots(bot, user_id, post_id)

    # боты с токеном проверяются по-настоящему; без токена — только по кнопке «Готово»
    if missing or not_started or (manual and not bots_confirmed):
        kb = InlineKeyboardBuilder()
        for s in missing:
            kb.button(text=f"📢 {s['title']}", url=s["link"])
        for s in not_started + manual:
            kb.button(text=f"🤖 {s['title']}", url=s["link"])
        kb.button(text="✅ Готово", callback_data=f"check:{post_id}")
        kb.adjust(1)
        what = []
        if missing:
            what.append("подпишитесь на каналы")
        if not_started or manual:
            what.append("запустите ботов")
        sent = await bot.send_message(
            user_id,
            f"Чтобы получить промт, {' и '.join(what)} ниже, затем нажмите «✅ Готово».",
            reply_markup=kb.as_markup(),
        )
        _trash.setdefault(user_id, []).append(sent.message_id)
        return

    with db() as c:
        c.execute("INSERT INTO claims(user_id, post_id, ts) VALUES(?,?,?)", (user_id, post_id, int(time.time())))
    await send_prompt(bot, user_id, post_id, post["prompt"])


# ───────────────────────────── Пользователи ─────────────────────────────

@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    await wipe(bot, m.chat.id)
    is_new = save_user(m)
    arg = (command.args or "").strip()

    if arg.startswith("ref") and arg[3:].isdigit() and is_new:
        await register_referral(bot, m.from_user.id, int(arg[3:]))
    elif arg.startswith("p") and arg[1:].isdigit():
        await process(bot, m.from_user.id, int(arg[1:]))
        return

    if m.from_user.id in ADMIN_IDS:
        await reply(m, "👋 Админ-панель:", reply_markup=admin_menu())
    else:
        await reply(m, menu_text(m.from_user.id), reply_markup=user_menu())


@router.callback_query(F.data.startswith("check:"))
async def cb_check(cb: CallbackQuery, bot: Bot):
    post_id = int(cb.data.split(":")[1])
    missing = await missing_channels(bot, cb.from_user.id, post_id)
    not_started, _ = await check_bots(bot, cb.from_user.id, post_id)
    if missing or not_started:
        names = ", ".join(s["title"] for s in missing + not_started)
        await cb.answer(f"Вы ещё не выполнили: {names} 🙈"[:200], show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await process(bot, cb.from_user.id, post_id, bots_confirmed=True)


# ───────────────────────────── Генерации: OpenAI ─────────────────────────────

class AIError(Exception):
    """Ошибка генерации. user_msg — текст для пользователя, admin_note — что показать админу."""

    def __init__(self, user_msg: str, admin_note: str = ""):
        super().__init__(user_msg)
        self.user_msg = user_msg
        self.admin_note = admin_note


def _ai_error(status: int, body: str) -> AIError:
    code, msg = "", body[:300]
    try:
        err = json.loads(body).get("error", {})
        code, msg = str(err.get("code") or err.get("type") or ""), str(err.get("message") or msg)
    except Exception:
        pass
    low = (code + " " + msg).lower()
    if "moderation" in low or "safety" in low or "content_policy" in low:
        return AIError("😔 Это фото не прошло проверку безопасности. Попробуйте другое фото.")
    note = f"OpenAI вернул {status} ({code}): {msg}"
    if status in (401, 403):
        note += "\nПроверьте OPENAI_API_KEY."
    elif status == 404 or "model" in low and "not" in low:
        note += "\nВозможно, модель недоступна: смените OPENAI_VISION_MODEL / OPENAI_IMAGE_MODEL."
    elif status == 429 or "quota" in low or "billing" in low:
        note += "\nПроверьте баланс и лимиты аккаунта OpenAI."
    return AIError("😔 Сервис генерации сейчас недоступен. Попробуйте позже — генерация не списана.", note)


PROMPT_SYSTEM = """You are an expert prompt engineer for ChatGPT image generation.
The user gives you a REFERENCE image. Write ONE ready-to-use prompt in {lang} that the user will paste into
ChatGPT together with a photo of their own face, so that ChatGPT recreates the reference image as exactly as
possible, but with the user's face.

Rules:
- Begin with an instruction: use the attached photo of the user's face as the identity reference; keep the face,
  skin tone, age and facial features exactly as in the attached photo (do not alter or beautify them), and place
  this person into the scene described below. If the reference has several people, the user replaces only the main subject.
- Then describe the reference in precise detail: composition and framing, camera angle and distance, lens look,
  pose and body position, hands and gesture, facial expression, hairstyle and styling, every clothing item and
  accessory (colors, fabrics, patterns), background and environment, props, lighting (direction, softness, color
  temperature), color palette and grading, mood, photographic or art style, depth of field, texture, grain and
  post-processing, and aspect ratio/orientation.
- Be concrete and specific, avoid vague words. Do not identify or name any real person; describe people only by
  visible traits.
- Output ONLY the prompt text: no preface, no explanations, no markdown headings."""


async def ai_make_prompt(img: bytes, mime: str) -> str:
    import aiohttp

    data_url = f"data:{mime};base64,{base64.b64encode(img).decode()}"
    body = {
        "model": OPENAI_VISION_MODEL,
        "messages": [
            {"role": "system", "content": PROMPT_SYSTEM.format(lang=PROMPT_LANGUAGE)},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Reference image. Write the prompt."},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                ],
            },
        ],
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as http:
            async with http.post(
                f"{OPENAI_BASE_URL}/chat/completions", json=body,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            ) as r:
                text = await r.text()
                if r.status != 200:
                    raise _ai_error(r.status, text)
    except aiohttp.ClientError as e:
        raise AIError("😔 Нет связи с сервисом генерации. Попробуйте позже — генерация не списана.", str(e))
    msg = json.loads(text)["choices"][0]["message"]
    out = (msg.get("content") or "").strip()
    if msg.get("refusal") or not out:
        raise AIError("😔 Не удалось разобрать эту картинку. Попробуйте другую.")
    return out


async def ai_make_photo(face: bytes, face_mime: str, ref: bytes, ref_mime: str, prompt: str) -> bytes:
    import aiohttp

    full = (
        "Image 1 is a photo of the user's face: the identity reference, keep the face exactly as in it. "
        "Image 2 is the reference picture to recreate (scene, pose, clothing, lighting, style). "
        "Recreate image 2 with the person from image 1.\n\n" + prompt
    )
    form = aiohttp.FormData()
    form.add_field("model", OPENAI_IMAGE_MODEL)
    form.add_field("prompt", full)
    form.add_field("size", OPENAI_IMAGE_SIZE)
    form.add_field("quality", OPENAI_IMAGE_QUALITY)
    if OPENAI_INPUT_FIDELITY:
        form.add_field("input_fidelity", OPENAI_INPUT_FIDELITY)
    form.add_field("image[]", face, filename="face", content_type=face_mime)
    form.add_field("image[]", ref, filename="reference", content_type=ref_mime)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=360)) as http:
            async with http.post(
                f"{OPENAI_BASE_URL}/images/edits", data=form,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            ) as r:
                text = await r.text()
                if r.status != 200:
                    raise _ai_error(r.status, text)
    except aiohttp.ClientError as e:
        raise AIError("😔 Нет связи с сервисом генерации. Попробуйте позже — генерация не списана.", str(e))
    try:
        return base64.b64decode(json.loads(text)["data"][0]["b64_json"])
    except Exception:
        raise AIError("😔 Сервис не вернул изображение. Попробуйте ещё раз — генерация не списана.", text[:300])


async def ai_check() -> str:
    """Проверка ключа и доступности моделей (команда /aicheck для админа)."""
    import aiohttp

    if not OPENAI_API_KEY:
        return "❌ OPENAI_API_KEY не задан."
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
            async with http.get(f"{OPENAI_BASE_URL}/models", headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}) as r:
                if r.status != 200:
                    return f"❌ Ключ не подошёл: {r.status} {(await r.text())[:200]}"
                ids = {m["id"] for m in (await r.json()).get("data", [])}
    except Exception as e:
        return f"❌ Нет связи с OpenAI: {e}"
    def mark(name):
        return "✅" if name in ids else "⚠️ нет в списке"

    return (
        "✅ Ключ работает.\n"
        f"Анализ картинки: <code>{OPENAI_VISION_MODEL}</code> {mark(OPENAI_VISION_MODEL)}\n"
        f"Генерация фото: <code>{OPENAI_IMAGE_MODEL}</code> {mark(OPENAI_IMAGE_MODEL)}\n"
        "Если модели нет в списке — смените OPENAI_VISION_MODEL / OPENAI_IMAGE_MODEL."
    )


# ───────────────────────────── Генерации: баланс, рефералы ─────────────────────────────

_busy: set[int] = set()  # кто сейчас ждёт генерацию (чтобы не слали несколько сразу)


def ensure_user(u) -> None:
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_seen) VALUES(?,?,?)",
            (u.id, u.username, int(time.time())),
        )


def balance(uid: int) -> tuple[int, int]:
    with db() as c:
        r = c.execute("SELECT free_gens, paid_gens FROM users WHERE user_id=?", (uid,)).fetchone()
    return (r["free_gens"] or 0, r["paid_gens"] or 0) if r else (0, 0)


def consume_gen(uid: int):
    """Списывает 1 генерацию: сначала бесплатную, потом платную. Возвращает 'free' / 'paid' / None."""
    with db() as c:
        if c.execute("UPDATE users SET free_gens=free_gens-1 WHERE user_id=? AND free_gens>0", (uid,)).rowcount:
            return "free"
        if c.execute("UPDATE users SET paid_gens=paid_gens-1 WHERE user_id=? AND paid_gens>0", (uid,)).rowcount:
            return "paid"
    return None


def refund_gen(uid: int, kind: str) -> None:
    col = "free_gens" if kind == "free" else "paid_gens"
    with db() as c:
        c.execute(f"UPDATE users SET {col}={col}+1 WHERE user_id=?", (uid,))


def fmt_price(p: float) -> str:
    return str(int(p)) if float(p).is_integer() else f"{p:.2f}"


async def register_referral(bot: Bot, new_id: int, ref_id: int) -> None:
    """Новый пользователь пришёл по ссылке друга: считаем приглашение и выдаём бонус."""
    if ref_id == new_id:
        return
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE user_id=?", (ref_id,)).fetchone():
            return
        c.execute("UPDATE users SET ref_by=? WHERE user_id=? AND ref_by IS NULL", (ref_id, new_id))
        invited = c.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (ref_id,)).fetchone()[0]
        given = c.execute("SELECT ref_bonus FROM users WHERE user_id=?", (ref_id,)).fetchone()[0] or 0
        add = max(0, min(invited // REF_FRIENDS_NEEDED, REF_MAX_BONUSES) - given)
        if add:
            c.execute("UPDATE users SET free_gens=free_gens+?, ref_bonus=ref_bonus+? WHERE user_id=?", (add, add, ref_id))
    try:
        if add:
            await bot.send_message(ref_id, f"🎉 Уже {invited} друга(зей) по вашей ссылке! Вам начислена "
                                           f"<b>+{add} бесплатная генерация</b> 🎁")
        else:
            await bot.send_message(ref_id, f"👥 По вашей ссылке присоединился друг ({invited}/{REF_FRIENDS_NEEDED}).")
    except Exception:
        pass


def menu_text(uid: int) -> str:
    free, paid = balance(uid)
    return (
        "👋 Привет!\n\nЯ создам <b>точный промт</b> по любой картинке. Отправьте этот промт в ChatGPT вместе со "
        "своим фото — и получите такую же картинку, но с вами.\n\n"
        f"🎁 Бесплатных генераций: <b>{free}</b>\n💎 Платных: <b>{paid}</b>\n\n"
        f"🎁 Пригласите {REF_FRIENDS_NEEDED} друзей — получите ещё одну бесплатную генерацию."
    )


def user_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎨 Создать свой промт", callback_data="gen:new")
    kb.button(text="📤 Поделиться", callback_data="menu:share")
    kb.button(text="💳 Купить генерации", callback_data="buy:menu")
    kb.adjust(1, 2)
    return kb.as_markup()


async def edit_or_send(cb: CallbackQuery, text: str, kb=None) -> int:
    """Редактирует сообщение с кнопкой; если нельзя (например, это файл) — шлёт новое. Возвращает id сообщения."""
    try:
        await cb.message.edit_text(text, reply_markup=kb)
        return cb.message.message_id
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return cb.message.message_id
    except Exception:
        pass
    sent = await cb.message.answer(text, reply_markup=kb)
    return sent.message_id


def bar(p: int) -> str:
    n = p // 10
    return "▓" * n + "░" * (10 - n)


async def with_progress(bot: Bot, chat_id: int, title: str, coro, est: float):
    """Показывает «живой» процент, пока выполняется coro. Процент приблизительный (оценка по времени)."""
    msg = await bot.send_message(chat_id, f"{title}\n\n{bar(0)} 0%")
    task = asyncio.ensure_future(coro)
    t0, last = time.time(), 0

    async def show(p: int) -> None:
        try:
            await bot.edit_message_text(f"{title}\n\n{bar(p)} {p}%", chat_id=chat_id, message_id=msg.message_id)
        except Exception:
            pass

    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=2.5)
            if done:
                break
            p = max(last, min(95, int(95 * (1 - math.exp(-(time.time() - t0) / est)))))
            if p != last:
                last = p
                await show(p)
        result = task.result()
        await show(100)
        await asyncio.sleep(0.5)
        return result
    finally:
        if not task.done():
            task.cancel()
        try:
            await bot.delete_message(chat_id, msg.message_id)
        except Exception:
            pass


async def fetch_image(bot: Bot, m: Message):
    """Достаёт картинку из сообщения: (байты, mime, file_id) или None."""
    if m.photo:
        fid, mime = m.photo[-1].file_id, "image/jpeg"
    elif m.document and (m.document.mime_type or "") in ("image/jpeg", "image/png", "image/webp"):
        if m.document.file_size and m.document.file_size > 10_000_000:
            return None
        fid, mime = m.document.file_id, m.document.mime_type
    else:
        return None
    buf = await bot.download(fid)
    return buf.getvalue(), mime, fid


async def ai_failed(bot: Bot, uid: int, e: Exception) -> None:
    if isinstance(e, AIError):
        user_msg, note = e.user_msg, e.admin_note
    else:
        log.exception("генерация: неожиданная ошибка")
        user_msg, note = "😔 Что-то пошло не так. Попробуйте позже — генерация не списана.", repr(e)
    await bot.send_message(uid, user_msg, reply_markup=user_menu())
    if note and time.time() - _err_notified.get(-1, 0) > 600:
        _err_notified[-1] = time.time()
        await notify_admins(bot, f"⚠️ Ошибка генерации:\n{html.escape(note)}")


# ───────────────────────────── Генерации: меню и «создать промт» ─────────────────────────────

@router.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await wipe(bot, m.chat.id)
    ensure_user(m.from_user)
    await reply(m, menu_text(m.from_user.id), reply_markup=user_menu())


@router.callback_query(F.data == "menu:home")
async def cb_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    ensure_user(cb.from_user)
    await edit_or_send(cb, menu_text(cb.from_user.id), user_menu())
    await cb.answer()


@router.callback_query(F.data == "menu:share")
async def cb_share(cb: CallbackQuery):
    uid = cb.from_user.id
    ensure_user(cb.from_user)
    link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"
    with db() as c:
        invited = c.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (uid,)).fetchone()[0]
    share = (f"https://t.me/share/url?url={quote(link)}"
             f"&text={quote('Бот делает точный промт по любой картинке 🎨 Получи бесплатную генерацию!')}")
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Поделиться с друзьями", url=share)
    kb.button(text="⬅️ Меню", callback_data="menu:home")
    kb.adjust(1)
    await edit_or_send(
        cb,
        f"📤 <b>Пригласите друзей</b>\n\nВаша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено друзей: <b>{invited}</b>\n"
        f"За {REF_FRIENDS_NEEDED} друзей — 🎁 +1 бесплатная генерация. Друг тоже получает бесплатную генерацию.",
        kb.as_markup(),
    )
    await cb.answer()


def buy_text(intro: str = "") -> str:
    lines = "\n".join(f"• {q} фото — {fmt_price(p)} сомони" for q, p in PACKAGES)
    return (
        f"{intro}💳 <b>Платные генерации</b>\n\n{lines}\n\n"
        "Платная генерация = точный промт + кнопка «🎨 Сгенерировать моё фото»: бот сам создаст картинку "
        "с вашим лицом по этому образцу.\n\nВыберите пакет:"
    )


def buy_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for q, p in PACKAGES:
        kb.button(text=f"{q} фото — {fmt_price(p)} сомони", callback_data=f"buy:{q}")
    kb.button(text="⬅️ Меню", callback_data="menu:home")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "gen:new")
async def cb_gen_new(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    ensure_user(cb.from_user)
    await state.clear()
    free, paid = balance(uid)
    if free + paid == 0:
        await edit_or_send(cb, buy_text("😔 Генерации закончились.\n\n"), buy_kb())
        return await cb.answer()
    if not OPENAI_API_KEY:
        return await cb.answer("Сервис временно недоступен. Попробуйте позже.", show_alert=True)
    await state.set_state(Create.ref)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu:home")]])
    await edit_or_send(
        cb,
        "📸 <b>Отправьте картинку, которую хотите повторить для своего фото.</b>\n\n"
        "Лучше хорошего качества — тогда промт получится точнее. Генерация спишется только после успешного результата.",
        kb,
    )
    await cb.answer()


@router.message(Create.ref, F.photo | F.document)
async def got_reference(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    if uid in _busy:
        return await m.answer("⏳ Подождите, предыдущая генерация ещё идёт.")
    img = await fetch_image(bot, m)
    if img is None:
        return await m.answer("Отправьте картинку как фото или файл JPG/PNG/WEBP до 10 МБ.")
    data, mime, fid = img
    kind = consume_gen(uid)
    await state.clear()
    if not kind:
        return await m.answer(buy_text("😔 Генерации закончились.\n\n"), reply_markup=buy_kb())
    _busy.add(uid)
    try:
        prompt = await with_progress(bot, uid, "🔍 Анализирую картинку и составляю промт…", ai_make_prompt(data, mime), est=35)
    except Exception as e:
        refund_gen(uid, kind)
        await ai_failed(bot, uid, e)
        return
    finally:
        _busy.discard(uid)
    with db() as c:
        gid = c.execute(
            "INSERT INTO gens(user_id, ref_file, ref_mime, prompt, paid, created) VALUES(?,?,?,?,?,?)",
            (uid, fid, mime, prompt, 1 if kind == "paid" else 0, int(time.time())),
        ).lastrowid
    free, paid = balance(uid)
    kb = InlineKeyboardBuilder()
    kb.button(text="🎨 Сгенерировать моё фото", callback_data=f"genphoto:{gid}")
    kb.button(text="➕ Создать ещё промт", callback_data="gen:new")
    kb.button(text="⬅️ Меню", callback_data="menu:home")
    kb.adjust(1)
    await bot.send_document(
        uid,
        BufferedInputFile(prompt.encode("utf-8-sig"), filename=f"prompt_{gid}.txt"),
        caption=(
            "✅ <b>Ваш промт готов</b> 💛\n\nОткройте файл, скопируйте текст и отправьте его в ChatGPT вместе "
            f"со своим фото, где хорошо видно лицо.\n\nОсталось: 🎁 {free} · 💎 {paid}"
        ),
        reply_markup=kb.as_markup(),
    )


@router.message(Create.ref)
async def ref_wrong(m: Message):
    await m.answer("Отправьте картинку (фото), которую хотите повторить 📸")


# ───────────────────────────── Генерации: «сгенерировать моё фото» (только платные) ─────────────────────────────

@router.callback_query(F.data.startswith("genphoto:"))
async def cb_genphoto(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[1])
    with db() as c:
        g = c.execute("SELECT * FROM gens WHERE id=?", (gid,)).fetchone()
    if not g or g["user_id"] != cb.from_user.id:
        return await cb.answer("Не нашёл эту генерацию.", show_alert=True)
    if not g["paid"]:
        await cb.answer("Создание фото доступно только на платных генерациях 💎", show_alert=True)
        await cb.message.answer(buy_text(), reply_markup=buy_kb())
        return
    if g["photo_used"]:
        return await cb.answer("Фото для этой генерации уже создано. Для новой — «Создать ещё промт».", show_alert=True)
    if not OPENAI_API_KEY:
        return await cb.answer("Сервис временно недоступен. Попробуйте позже.", show_alert=True)
    await state.set_state(Create.face)
    await state.update_data(gen_id=gid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu:home")]])
    await cb.message.answer(
        "🤳 <b>Отправьте своё фото, где хорошо видно лицо.</b>\n\n"
        "Лучше анфас, чётко, при хорошем освещении, без очков и фильтров. Используйте только свои фото.",
        reply_markup=kb,
    )
    await cb.answer()


@router.message(Create.face, F.photo | F.document)
async def got_face(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    if uid in _busy:
        return await m.answer("⏳ Подождите, предыдущая генерация ещё идёт.")
    gid = (await state.get_data()).get("gen_id")
    face = await fetch_image(bot, m)
    if face is None:
        return await m.answer("Отправьте фото как картинку или файл JPG/PNG/WEBP до 10 МБ.")
    with db() as c:
        g = c.execute("SELECT * FROM gens WHERE id=? AND user_id=? AND paid=1", (gid, uid)).fetchone()
        # бронируем: одна платная генерация — одно фото
        got = g and c.execute("UPDATE gens SET photo_used=1 WHERE id=? AND photo_used=0", (gid,)).rowcount
    await state.clear()
    if not got:
        return await m.answer("Фото для этой генерации уже создано или она недоступна.", reply_markup=user_menu())
    _busy.add(uid)
    try:
        ref_buf = await bot.download(g["ref_file"])
        photo = await with_progress(
            bot, uid, "🎨 Создаю ваше фото…",
            ai_make_photo(face[0], face[1], ref_buf.getvalue(), g["ref_mime"] or "image/jpeg", g["prompt"]),
            est=70,
        )
    except Exception as e:
        with db() as c:
            c.execute("UPDATE gens SET photo_used=0 WHERE id=?", (gid,))
        await ai_failed(bot, uid, e)
        return
    finally:
        _busy.discard(uid)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать ещё", callback_data="gen:new")
    kb.button(text="⬅️ Меню", callback_data="menu:home")
    kb.adjust(1)
    caption = "✨ <b>Готово!</b> Ваше фото по выбранному образцу."
    try:
        await bot.send_photo(uid, BufferedInputFile(photo, filename="photo.png"), caption=caption, reply_markup=kb.as_markup())
    except Exception:
        await bot.send_document(uid, BufferedInputFile(photo, filename="photo.png"), caption=caption, reply_markup=kb.as_markup())


@router.message(Create.face)
async def face_wrong(m: Message):
    await m.answer("Отправьте своё фото, где хорошо видно лицо 🤳")


# ───────────────────────────── Оплата: чек и подтверждение админом ─────────────────────────────

_pay_msgs: dict[int, list[tuple[int, int]]] = {}  # payment_id -> [(admin_id, message_id)]


def pay_kb(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"payok:{pid}"),
        InlineKeyboardButton(text="❌ Отказ", callback_data=f"payno:{pid}"),
    ]])


@router.callback_query(F.data == "buy:menu")
async def cb_buy_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    ensure_user(cb.from_user)
    await edit_or_send(cb, buy_text(), buy_kb())
    await cb.answer()


@router.callback_query(F.data.regexp(r"^buy:\d+$"))
async def cb_buy(cb: CallbackQuery, state: FSMContext):
    qty = int(cb.data.split(":")[1])
    price = dict(PACKAGES).get(qty)
    if price is None:
        return await cb.answer()
    ensure_user(cb.from_user)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="buy:menu")]])
    mid = await edit_or_send(
        cb,
        f"💳 <b>Оплата: {qty} фото — {fmt_price(price)} сомони</b>\n\n"
        f"1️⃣ Отправьте <b>{fmt_price(price)} сомони</b> на номер:\n📱 <code>{PAY_PHONE}</code>\n({PAY_METHODS})\n\n"
        "2️⃣ После оплаты отправьте сюда <b>чек</b> (скриншот или фото).",
        kb,
    )
    await state.set_state(Pay.receipt)
    await state.update_data(qty=qty, amount=price, instr=mid)
    await cb.answer()


@router.message(Pay.receipt, F.photo | F.document)
async def got_receipt(m: Message, state: FSMContext, bot: Bot):
    if m.document and not (m.document.mime_type or "").startswith("image/"):
        return await m.answer("Отправьте чек как фото или скриншот 🧾")
    data = await state.get_data()
    await state.clear()
    uid = m.from_user.id
    fid, kind = (m.photo[-1].file_id, "photo") if m.photo else (m.document.file_id, "doc")
    with db() as c:
        pid = c.execute(
            "INSERT INTO payments(user_id, username, qty, amount, receipt, receipt_kind, created) VALUES(?,?,?,?,?,?,?)",
            (uid, m.from_user.username, data["qty"], data["amount"], fid, kind, int(time.time())),
        ).lastrowid
    try:  # сообщение с реквизитами удаляется
        await bot.delete_message(uid, data["instr"])
    except Exception:
        pass
    await m.answer("⏳ <b>Ваш платёж обрабатывается.</b>\nПожалуйста, подождите — это может занять до 20 минут.")
    who = html.escape(m.from_user.full_name) + (f" (@{m.from_user.username})" if m.from_user.username else "")
    caption = (f"💳 <b>Платёж #{pid}</b>\nОт: {who}\nID: <code>{uid}</code>\n"
               f"Пакет: {data['qty']} фото — {fmt_price(data['amount'])} сомони")
    _pay_msgs[pid] = []
    for aid in ADMIN_IDS:
        try:
            send = bot.send_photo if kind == "photo" else bot.send_document
            sent = await send(aid, fid, caption=caption, reply_markup=pay_kb(pid))
            _pay_msgs[pid].append((aid, sent.message_id))
        except Exception as e:
            log.warning("не смог отправить чек админу %s: %s", aid, e)


@router.message(Pay.receipt)
async def receipt_wrong(m: Message):
    await m.answer("Отправьте чек как фото или скриншот 🧾")


async def _finish_payment(cb: CallbackQuery, bot: Bot, approve: bool) -> None:
    pid = int(cb.data.split(":")[1])
    with db() as c:
        changed = c.execute(
            "UPDATE payments SET status=? WHERE id=? AND status='pending'", ("ok" if approve else "rejected", pid)
        ).rowcount
        row = c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if changed and approve:
            c.execute("UPDATE users SET paid_gens=paid_gens+? WHERE user_id=?", (row["qty"], row["user_id"]))
    if not changed:
        await cb.answer("Этот платёж уже обработан.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    status = f"✅ Подтверждён ({html.escape(cb.from_user.full_name)})" if approve else f"❌ Отклонён ({html.escape(cb.from_user.full_name)})"
    for aid, mid in _pay_msgs.pop(pid, [(cb.message.chat.id, cb.message.message_id)]):
        try:
            base = (cb.message.caption or "") if (aid, mid) == (cb.message.chat.id, cb.message.message_id) else ""
            await bot.edit_message_reply_markup(chat_id=aid, message_id=mid, reply_markup=None)
            if base:
                await bot.edit_message_caption(chat_id=aid, message_id=mid, caption=f"{html.escape(base)}\n\n{status}")
        except Exception:
            pass
    try:
        if approve:
            _, paid = balance(row["user_id"])
            kb = InlineKeyboardBuilder()
            kb.button(text="🎨 Создать свой промт", callback_data="gen:new")
            await bot.send_message(
                row["user_id"],
                f"✅ <b>Платёж подтверждён!</b>\nНачислено: <b>{row['qty']}</b> платных генераций 💎\nБаланс: {paid}",
                reply_markup=kb.as_markup(),
            )
        else:
            await bot.send_message(
                row["user_id"],
                "❌ Платёж не подтверждён. Если вы оплатили — отправьте чек ещё раз через «💳 Купить генерации» "
                "или напишите администратору.",
            )
    except Exception:
        pass
    await cb.answer("Готово")


@router.callback_query(F.data.startswith("payok:"), ADMIN)
async def cb_payok(cb: CallbackQuery, bot: Bot):
    await _finish_payment(cb, bot, True)


@router.callback_query(F.data.startswith("payno:"), ADMIN)
async def cb_payno(cb: CallbackQuery, bot: Bot):
    await _finish_payment(cb, bot, False)


@router.message(Command("give"), ADMIN)
async def cmd_give(m: Message, command: CommandObject, bot: Bot):
    """/give <user_id> <число> — начислить платные генерации вручную (например, при сбое оплаты)."""
    try:
        uid, n = (int(x) for x in (command.args or "").split())
    except ValueError:
        return await reply(m, "Формат: <code>/give USER_ID КОЛ-ВО</code>")
    with db() as c:
        ok = c.execute("UPDATE users SET paid_gens=MAX(0, paid_gens+?) WHERE user_id=?", (n, uid)).rowcount
    if not ok:
        return await reply(m, "Такого пользователя нет в базе.")
    await reply(m, f"✅ Пользователю {uid} начислено платных генераций: {n}. Баланс: {balance(uid)[1]}")


@router.message(Command("aicheck"), ADMIN)
async def cmd_aicheck(m: Message):
    await reply(m, await ai_check())


# ───────────────────────────── Админка ─────────────────────────────

@router.message(Command("admin"), ADMIN)
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await wipe(m.bot, m.chat.id)
    await reply(m, "👋 Админ-панель:", reply_markup=admin_menu())


@router.message(Command("backup"), ADMIN)
async def cmd_backup(m: Message):
    await reply(m, await gh_backup(force=True))


@router.message(Command("cancel"), ADMIN)
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await wipe(m.bot, m.chat.id)
    await reply(m, "Отменено.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:menu", ADMIN)
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("👋 Админ-панель:", reply_markup=admin_menu())
    await cb.answer()


@router.callback_query(F.data == "adm:stats", ADMIN)
async def cb_stats(cb: CallbackQuery):
    with db() as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        posts = c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        claims = c.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        uniq = c.execute("SELECT COUNT(DISTINCT user_id) FROM claims").fetchone()[0]
        gens = c.execute("SELECT COUNT(*), COALESCE(SUM(photo_used),0) FROM gens").fetchone()
        pays = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE status='ok'").fetchone()
        pend = c.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
    await cb.message.edit_text(
        f"📊 <b>Статистика</b>\n\nПользователей: {users}\nПубликаций: {posts}\n"
        f"Выдач промта: {claims} (уникальных людей: {uniq})\n\n"
        f"🎨 Создано промтов: {gens[0]}, фото сгенерировано: {gens[1]}\n"
        f"💳 Платежей подтверждено: {pays[0]} на {fmt_price(pays[1])} сомони, ждут решения: {pend}",
        reply_markup=back_menu(),
    )
    await cb.answer()


# ---------- Публикации ----------

@router.callback_query(F.data == "adm:posts", ADMIN)
async def cb_posts(cb: CallbackQuery):
    with db() as c:
        rows = c.execute(
            """SELECT p.id, p.channel_id,
                      (SELECT COUNT(*) FROM claims WHERE post_id=p.id) AS n,
                      (SELECT COUNT(*) FROM post_subs WHERE post_id=p.id) AS s
               FROM posts p ORDER BY p.id DESC LIMIT 15"""
        ).fetchall()
    if not rows:
        await cb.message.edit_text("Публикаций пока нет.", reply_markup=back_menu())
        return await cb.answer()
    kb = InlineKeyboardBuilder()
    lines = ["📋 <b>Последние публикации</b>\n"]
    for r in rows:
        names = ", ".join(html.escape(x["title"]) for x in post_subs(r["id"])) or "нет ⚠️"
        lines.append(f"#{r['id']} → {r['channel_id']} · выдач: {r['n']}\n     подписки: {names}")
        kb.button(text=f"✏️ Подписки #{r['id']}", callback_data=f"postsubs:{r['id']}")
        kb.button(text=f"🗑 #{r['id']}", callback_data=f"postdel:{r['id']}")
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    kb.adjust(*([2] * len(rows)), 1)
    await cb.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await cb.answer()


async def show_post_subs(cb: CallbackQuery, pid: int) -> None:
    with db() as c:
        catalog = c.execute("SELECT * FROM subs ORDER BY id").fetchall()
    if not catalog:
        await cb.message.edit_text(
            "В каталоге нет каналов и ботов. Добавьте их в «📢 Каналы и боты для подписки».",
            reply_markup=back_menu(),
        )
        return
    chosen = {x["id"] for x in post_subs(pid)}
    kb = InlineKeyboardBuilder()
    for x in catalog:
        icon = "📢" if x["kind"] == "channel" else "🤖"
        mark = "✅" if x["id"] in chosen else "▫️"
        kb.button(text=f"{mark} {icon} {x['title']}", callback_data=f"pst:{pid}:{x['id']}")
    kb.button(text="⬅️ К публикациям", callback_data="adm:posts")
    kb.adjust(1)
    await cb.message.edit_text(
        f"Подписки для публикации #{pid}.\nНажмите на канал/бота, чтобы включить или выключить. "
        "Изменения действуют сразу.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("postsubs:"), ADMIN)
async def cb_postsubs(cb: CallbackQuery):
    await show_post_subs(cb, int(cb.data.split(":")[1]))
    await cb.answer()


@router.callback_query(F.data.startswith("pst:"), ADMIN)
async def cb_pst(cb: CallbackQuery):
    _, pid, sid = cb.data.split(":")
    with db() as c:
        has = c.execute("SELECT 1 FROM post_subs WHERE post_id=? AND sub_id=?", (pid, sid)).fetchone()
        if has:
            c.execute("DELETE FROM post_subs WHERE post_id=? AND sub_id=?", (pid, sid))
        else:
            c.execute("INSERT OR IGNORE INTO post_subs(post_id, sub_id) VALUES(?,?)", (pid, sid))
    await show_post_subs(cb, int(pid))
    await cb.answer()


@router.callback_query(F.data.startswith("postdel:"), ADMIN)
async def cb_postdel(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[1])
    with db() as c:
        p = c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        c.execute("DELETE FROM posts WHERE id=?", (pid,))
        c.execute("DELETE FROM post_subs WHERE post_id=?", (pid,))
    if p and p["message_id"]:
        try:
            await bot.delete_message(p["channel_id"], p["message_id"])
        except Exception:
            pass
    await cb.answer("Удалено (и из канала, если получилось)")
    await cb_posts(cb)


@router.callback_query(F.data == "adm:newpost", ADMIN)
async def cb_newpost(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewPost.content)
    await cb.message.edit_text(
        "1/4. Пришлите <b>текст публикации</b> для канала (можно с фото и форматированием).\n"
        "Отмена: /cancel"
    )
    await cb.answer()


@router.message(NewPost.content, ADMIN)
async def np_content(m: Message, state: FSMContext):
    if m.photo:
        await state.update_data(text=m.html_text if m.caption else "", photo=m.photo[-1].file_id)
    elif m.text:
        await state.update_data(text=m.html_text, photo=None)
    else:
        return await reply(m, "Нужен текст или фото с подписью.")
    await state.set_state(NewPost.prompt)
    await reply(m, 
        "2/4. Пришлите <b>промт для этой публикации</b> обычным текстом (без фото) "
        "или файлом <code>.txt</code>, если он очень длинный. "
        "Его не будет в канале — пользователь получит его в боте после нажатия кнопки "
        "(длинный промт придёт текстовым файлом)."
    )


def picker_kb(catalog: list[sqlite3.Row], chosen: set[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in catalog:
        icon = "📢" if s["kind"] == "channel" else "🤖"
        mark = "✅" if s["id"] in chosen else "▫️"
        kb.button(text=f"{mark} {icon} {s['title']}", callback_data=f"ps:t:{s['id']}")
    kb.button(text="➡️ Далее", callback_data="ps:ok")
    kb.adjust(1)
    return kb.as_markup()


@router.message(NewPost.prompt, ADMIN, F.text | F.document)
async def np_prompt(m: Message, state: FSMContext, bot: Bot):
    if m.document:
        if m.document.file_size and m.document.file_size > 1_000_000:
            return await reply(m, "Файл слишком большой (максимум 1 МБ).")
        buf = await bot.download(m.document)
        raw = buf.getvalue()
        try:
            prompt = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                prompt = raw.decode("cp1251")
            except UnicodeDecodeError:
                return await reply(m, "Не смог прочитать файл. Пришлите текстовый файл .txt в UTF-8.")
        prompt = prompt.strip()
        if not prompt:
            return await reply(m, "Файл пустой.")
    else:
        prompt = m.text
    await state.update_data(prompt=prompt, chosen=[], warned=False)
    with db() as c:
        catalog = c.execute("SELECT * FROM subs ORDER BY id").fetchall()
    if not catalog:
        await state.set_state(NewPost.channel)
        return await reply(m, 
            "3/4. В каталоге нет каналов/ботов для подписки — эта публикация будет без подписки "
            "(добавить их можно в меню «Каналы и боты»).\n\n"
            "4/4. Куда публиковать? Пришлите <code>@username</code> канала, ID или перешлите сообщение из канала."
        )
    await state.set_state(NewPost.subs)
    await reply(m, 
        "3/4. На что нужно подписаться <b>для этой публикации</b>? Отметьте и нажмите «Далее» "
        "(можно не выбирать ничего).",
        reply_markup=picker_kb(catalog, set()),
    )


@router.callback_query(NewPost.subs, F.data.startswith("ps:t:"), ADMIN)
async def np_toggle(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    data = await state.get_data()
    chosen = set(data.get("chosen", []))
    chosen.symmetric_difference_update({sid})
    await state.update_data(chosen=list(chosen))
    with db() as c:
        catalog = c.execute("SELECT * FROM subs ORDER BY id").fetchall()
    await cb.message.edit_reply_markup(reply_markup=picker_kb(catalog, chosen))
    await cb.answer()


@router.callback_query(NewPost.subs, F.data == "ps:ok", ADMIN)
async def np_subs_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("chosen") and not data.get("warned"):
        await state.update_data(warned=True)
        await cb.answer(
            "Вы ничего не выбрали — подписка для этой публикации не потребуется. "
            "Нажмите «Далее» ещё раз, если так и задумано.",
            show_alert=True,
        )
        return
    await state.set_state(NewPost.channel)
    await cb.message.edit_text(
        "4/4. Куда публиковать? Пришлите <code>@username</code> канала или числовой ID, "
        "либо перешлите любое сообщение из канала.\nБот должен быть админом канала с правом публикации."
    )
    await cb.answer()


async def resolve_chat_ref(m: Message):
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "chat", None) is not None:
        return fo.chat.id
    if m.text:
        t = m.text.strip()
        if t.lstrip("-").isdigit():
            return int(t)
        if t.startswith("@"):
            return t
        if "t.me/" in t:
            return "@" + t.rstrip("/").split("/")[-1]
    return None


@router.message(NewPost.channel, ADMIN)
async def np_channel(m: Message, state: FSMContext, bot: Bot):
    chat_ref = await resolve_chat_ref(m)
    if chat_ref is None:
        return await reply(m, "Не понял. Пришлите @username, ID или перешлите сообщение из канала.")
    data = await state.get_data()

    with db() as c:
        cur = c.execute(
            "INSERT INTO posts(text, photo, prompt, channel_id, created) VALUES(?,?,?,?,?)",
            (data.get("text") or "", data.get("photo"), data["prompt"], str(chat_ref), int(time.time())),
        )
        post_id = cur.lastrowid
        for sid in data.get("chosen", []):
            c.execute("INSERT OR IGNORE INTO post_subs(post_id, sub_id) VALUES(?,?)", (post_id, sid))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🎁 Получить промт", url=f"https://t.me/{BOT_USERNAME}?start=p{post_id}")
        ]]
    )
    try:
        if data.get("photo"):
            sent = await bot.send_photo(chat_ref, data["photo"], caption=data.get("text") or None, reply_markup=kb)
        else:
            sent = await bot.send_message(chat_ref, data.get("text") or "🎁", reply_markup=kb)
    except Exception as e:
        with db() as c:
            c.execute("DELETE FROM posts WHERE id=?", (post_id,))
            c.execute("DELETE FROM post_subs WHERE post_id=?", (post_id,))
        return await reply(m, f"❌ Не удалось опубликовать: {html.escape(str(e))}\nПроверьте, что бот — админ канала.")

    with db() as c:
        c.execute("UPDATE posts SET message_id=?, channel_id=? WHERE id=?", (sent.message_id, str(sent.chat.id), post_id))
    await state.clear()
    await wipe(bot, m.chat.id)
    await reply(m, f"✅ Опубликовано (публикация #{post_id}).", reply_markup=admin_menu())


# ---------- Каталог каналов и ботов ----------

@router.callback_query(F.data == "adm:subs", ADMIN)
async def cb_subs(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    with db() as c:
        rows = c.execute("SELECT * FROM subs ORDER BY id").fetchall()
    kb = InlineKeyboardBuilder()
    lines = ["📢 <b>Каналы и боты</b>\nЗдесь вы их добавляете, а при создании публикации выбираете, "
             "что нужно именно для неё.\n"]
    if not rows:
        lines.append("Пока пусто.")
    for r in rows:
        icon = "📢" if r["kind"] == "channel" else "🤖"
        note = ""
        if r["kind"] == "bot":
            note = " — запуск проверяется ✅" if r["token"] else " — без проверки (только «Готово»)"
        lines.append(f"{icon} {html.escape(r['title'])}{note}")
        row = [InlineKeyboardButton(text=f"🗑 {r['title'][:24]}", callback_data=f"subdel:{r['id']}")]
        if r["kind"] == "bot" and not r["token"]:
            row.append(InlineKeyboardButton(text="🔑 Токен", callback_data=f"subtoken:{r['id']}"))
        kb.row(*row)
    kb.row(
        InlineKeyboardButton(text="➕ Канал", callback_data="subadd:channel"),
        InlineKeyboardButton(text="➕ Бот", callback_data="subadd:bot"),
    )
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu"))
    await cb.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("subdel:"), ADMIN)
async def cb_subdel(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    with db() as c:
        c.execute("DELETE FROM subs WHERE id=?", (sid,))
        c.execute("DELETE FROM post_subs WHERE sub_id=?", (sid,))
    await cb.answer("Удалено")
    await cb_subs(cb, state)


@router.callback_query(F.data == "subadd:channel", ADMIN)
async def cb_subadd_channel(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.wait)
    await cb.message.edit_text(
        "Пришлите <code>@username</code> канала, числовой ID или перешлите сообщение из канала.\n"
        "⚠️ Бот должен быть <b>админом</b> этого канала — иначе он не сможет проверять подписку.\n/cancel — отмена"
    )
    await cb.answer()


@router.message(AddChannel.wait, ADMIN)
async def add_channel(m: Message, state: FSMContext, bot: Bot):
    ref = await resolve_chat_ref(m)
    if ref is None:
        return await reply(m, "Не понял. Пришлите @username, ID или пересланное сообщение.")
    try:
        chat = await bot.get_chat(ref)
        me = await bot.get_chat_member(chat.id, bot.id)
        if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            return await reply(m, "Сделайте бота админом канала и пришлите канал ещё раз.")
        if chat.username:
            link = f"https://t.me/{chat.username}"
        else:
            link = (await bot.create_chat_invite_link(chat.id)).invite_link
    except Exception as e:
        return await reply(m, f"❌ Не получилось: {html.escape(str(e))}")
    with db() as c:
        c.execute("INSERT INTO subs(kind, chat_id, title, link) VALUES('channel',?,?,?)", (chat.id, chat.title or str(chat.id), link))
    await state.clear()
    await wipe(bot, m.chat.id)
    await reply(m, f"✅ Канал «{html.escape(chat.title or '')}» добавлен.", reply_markup=admin_menu())


@router.callback_query(F.data == "subadd:bot", ADMIN)
async def cb_subadd_bot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddBot.wait)
    await cb.message.edit_text(
        "Пришлите <code>@username</code> бота или ссылку на него (можно с параметром, "
        "например <code>https://t.me/mybot?start=abc</code>).\n/cancel — отмена"
    )
    await cb.answer()


TOKEN_ASK = (
    "🔑 Хотите проверять запуск этого бота по-настоящему?\n"
    "Пришлите <b>токен этого бота</b> от @BotFather (вид <code>123456:ABC...</code>) — "
    "и бот будет знать, запускал ли его человек. Сообщение с токеном я сразу удалю.\n\n"
    "Подходит только для <b>ваших</b> ботов. Для чужих нажмите «Пропустить» — "
    "тогда подтверждением служит кнопка «✅ Готово»."
)


def skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏭ Пропустить", callback_data="bottoken:skip")]]
    )


async def say(bot: Bot, chat_id: int, text: str, **kw) -> None:
    """Сообщение, которое потом уберёт wipe()."""
    sent = await bot.send_message(chat_id, text, **kw)
    _trash.setdefault(chat_id, []).append(sent.message_id)


@router.message(AddBot.wait, ADMIN, F.text)
async def add_bot(m: Message, state: FSMContext):
    t = m.text.strip()
    if t.startswith("@"):
        username, link = t[1:], f"https://t.me/{t[1:]}"
    elif "t.me/" in t:
        link = t if t.startswith("http") else "https://" + t
        username = t.split("t.me/")[1].split("?")[0].strip("/")
    else:
        return await reply(m, "Пришлите @username или ссылку вида https://t.me/mybot")
    await state.update_data(username=username, link=link, sub_id=None)
    await state.set_state(AddBot.token)
    await reply(m, TOKEN_ASK, reply_markup=skip_kb())


async def finish_bot(bot: Bot, chat_id: int, state: FSMContext, token: str | None) -> None:
    data = await state.get_data()
    with db() as c:
        if data.get("sub_id"):
            c.execute("UPDATE subs SET token=? WHERE id=?", (token, data["sub_id"]))
            text = "✅ Токен сохранён — запуск бота теперь проверяется."
        else:
            c.execute(
                "INSERT INTO subs(kind, chat_id, title, link, token) VALUES('bot',NULL,?,?,?)",
                (f"@{data['username']}", data["link"], token),
            )
            text = f"✅ Бот @{html.escape(data['username'])} добавлен" + (
                " (запуск проверяется)." if token else " (без проверки запуска).")
    await state.clear()
    await wipe(bot, chat_id)
    await say(bot, chat_id, text, reply_markup=admin_menu())


@router.message(AddBot.token, ADMIN, F.text)
async def add_bot_token(m: Message, state: FSMContext, bot: Bot):
    token = m.text.strip()
    data = await state.get_data()
    try:
        tmp = Bot(token)
        try:
            me = await tmp.get_me()
        finally:
            await tmp.session.close()
    except Exception:
        return await reply(m, "❌ Токен не подошёл. Проверьте его в @BotFather и пришлите ещё раз "
                              "или нажмите «Пропустить».", reply_markup=skip_kb())
    expected = data.get("username")
    if expected is None and data.get("sub_id"):
        with db() as c:
            row = c.execute("SELECT title FROM subs WHERE id=?", (data["sub_id"],)).fetchone()
        expected = (row["title"] if row else "").lstrip("@")
    if expected and (me.username or "").lower() != expected.lower():
        return await reply(m, f"❌ Этот токен от бота @{html.escape(me.username or '?')}, а нужен @{html.escape(expected)}. "
                              "Пришлите правильный токен или нажмите «Пропустить».", reply_markup=skip_kb())
    await finish_bot(bot, m.chat.id, state, token)


@router.callback_query(AddBot.token, F.data == "bottoken:skip", ADMIN)
async def cb_bot_skip(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await cb.answer()
    if data.get("sub_id"):  # добавление токена к существующему боту — просто отмена
        await state.clear()
        await wipe(bot, cb.message.chat.id)
        await say(bot, cb.message.chat.id, "Отменено.", reply_markup=admin_menu())
        return
    await finish_bot(bot, cb.message.chat.id, state, None)


@router.callback_query(F.data.startswith("subtoken:"), ADMIN)
async def cb_subtoken(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    await state.update_data(sub_id=sid, username=None, link=None)
    await state.set_state(AddBot.token)
    await cb.message.edit_text(TOKEN_ASK, reply_markup=skip_kb())
    await cb.answer()


# ───────────────────────────── Запуск ─────────────────────────────

async def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — админ-панель будет недоступна")
    await gh_restore()
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот @%s запущен", BOT_USERNAME)
    task = None
    if GH_ENABLED:
        log.info("GitHub-бэкап включён: %s", GITHUB_REPO)
        task = asyncio.create_task(backup_loop())
    else:
        log.warning("GitHub-бэкап выключен — данные хранятся только в %s", DB_PATH)
    try:
        await dp.start_polling(bot)
    finally:
        if task:
            task.cancel()
            await gh_backup()  # финальное сохранение при остановке


if __name__ == "__main__":
    asyncio.run(main())
