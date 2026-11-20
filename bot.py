"""
Telegram-бот «Получить промт» (aiogram 3.x)

- Админ создаёт публикацию: текст/фото для канала + СВОЙ промт (в канале его нет)
  + выбирает, на какие каналы/ботов нужно подписаться именно для этой публикации.
- Бот публикует пост в канал с кнопкой «Получить промт» (deep-link ?start=p<ID>).
- Пользователь нажимает кнопку -> бот определяет публикацию, проверяет подписки этой публикации
  и присылает «Ваш промт 💛» + промт текстом (без фото) в <code>-блоке: нажатие копирует.
  Если промт длиннее PROMPT_FILE_THRESHOLD символов — присылает его текстовым файлом .txt.

Мини-приложение (webapp.html): если задан WEBAPP_URL (публичный https-адрес бота), промт выдаётся не в чат,
а в Telegram Mini App — бот сам отдаёт webapp.html и API (aiohttp, порт PORT).

Второй бот (публикации в каналах, тарифы FREE/STANDART/PRO) включается переменной PUBLISH_BOT_TOKEN
— он работает в этом же процессе параллельно с первым (BOT_TOKEN). Админы второго бота: PUB_ADMIN_IDS (иначе ADMIN_IDS).

Переменные окружения: BOT_TOKEN, ADMIN_IDS, DB_PATH, WEBAPP_URL, PORT (см. README.md)
FEATURES_LOCKED=1 (по умолчанию) — кнопки «Создать свой промт», «Купить генерации», «Мой баланс»
показывают 🔒 и сообщение «функция в разработке». FEATURES_LOCKED=0 — снять замок.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import math
import logging
import os
import re
import sqlite3
import tempfile
import time
from urllib.parse import parse_qsl, quote

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
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
    MenuButtonWebApp,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("promt-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DATA_DIR = os.getenv("DATA_DIR", "")  # постоянная папка хостинга (если есть), например /app/data
DB_PATH = os.getenv("DB_PATH") or (os.path.join(DATA_DIR, "bot.db") if DATA_DIR else "bot.db")
# промт длиннее этого числа символов отправляется текстовым файлом (.txt), а не сообщением
FILE_THRESHOLD = int(os.getenv("PROMPT_FILE_THRESHOLD", "1500"))

# ── Замок на функциях (1 — закрыто, 0 — открыто) ──
FEATURES_LOCKED = os.getenv("FEATURES_LOCKED", "1") == "1"
LOCK_TEXT = ("🔒 Эта функция в разработке.\n"
             "Когда она станет доступна, мы сообщим вам. Спасибо за пользование ☺️")

# ── Генерации: OpenAI, оплата, рефералы ──
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-5.4-mini")  # анализ картинки -> промт
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")  # генерация фото
OPENAI_IMAGE_SIZE = os.getenv("OPENAI_IMAGE_SIZE", "1024x1536")
OPENAI_IMAGE_QUALITY = os.getenv("OPENAI_IMAGE_QUALITY", "medium")
# OpenRouter: бесплатные модели с картинками для написания промта (необязательно; фото всё равно делает OpenAI)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")  # авто-выбор бесплатной модели с поддержкой картинок
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

# ── Реклама бота-скачивателя и подпись автора ──
SAVER_URL = os.getenv("SAVER_URL", "https://t.me/Taj_Saverbot")
OWNER_URL = os.getenv("OWNER_URL", "https://t.me/IDIEVW")
OWNER_TAG = os.getenv("OWNER_TAG", "@IDIEVW")
# картинка для рекламы: файл рядом с bot.py (по умолчанию saver_promo.jpg), либо file_id/ссылка в SAVER_PROMO_PHOTO
SAVER_PROMO_PHOTO = os.getenv("SAVER_PROMO_PHOTO", "") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "saver_promo.jpg")
# ── Мини-приложение «PROMPT TAJ» ──
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")  # например https://mybot.bothost.ru ; пусто — мини-апп выключен
PORT = int(os.getenv("PORT", "8080"))
WEBAPP_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp.html")
SIGN = f'<a href="{OWNER_URL}">{html.escape(OWNER_TAG)}</a>'  # подпись внизу каждого промта (кликабельная)
SAVER_TEXT = (
    "🎬 <b>Скачивайте видео из Instagram и YouTube</b>\n\n"
    "Наш бот скачает видео по ссылке и умеет превращать видео в музыку 🎵\n"
    "Быстро, без водяных знаков и бесплатно."
)
TIP_SINGLE = ("💡 <b>Совет:</b> загружай одновременно 2 фотографии: свою фото, а также это фото как референс. "
              "Так генератору будет проще сохранить ваше лицо и повторить позу.")
TIP_PAIR = ("💡 <b>Совет:</b> загружай одновременно 2 фотографии: свою и девушки, а также это фото как референс. "
            "Так генератору будет проще сохранить ваши лица и повторить позу.")
_saver_photo_id = ""  # file_id после первой отправки — дальше фото не загружается заново

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
            CREATE TABLE IF NOT EXISTS favorites(
                user_id INTEGER,
                post_id INTEGER,
                ts INTEGER,
                PRIMARY KEY(user_id, post_id)
            );
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
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
            ("kb_shown", "INTEGER DEFAULT 0"),  # показывали ли постоянные кнопки внизу
            ("first_name", "TEXT"),
            ("webapp_at", "INTEGER"),  # когда впервые открыл мини-приложение
        ):
            if name not in ucols:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        pcols = [r[1] for r in c.execute("PRAGMA table_info(posts)")]
        if "kind" not in pcols:  # 'single' — одиночка, 'pair' — пара
            c.execute("ALTER TABLE posts ADD COLUMN kind TEXT DEFAULT 'single'")
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
    kind = State()
    subs = State()
    channel = State()


class Create(StatesGroup):
    ref = State()   # ждём картинку-образец
    face = State()  # ждём фото лица для генерации


class Pay(StatesGroup):
    receipt = State()  # ждём чек


class SetPromo(StatesGroup):
    wait = State()  # ждём фото (или пересланный пост канала) для рекламы


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
    kb.button(text="🖼 Фото рекламы бота", callback_data="adm:setpromo")
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
CLEAN_CMDS = {"/setpromo", "/start", "/admin", "/cancel", "/backup", "/menu", "/give", "/aicheck"}


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
        pressed = (event.text or "") in BUTTON_TEXTS
        if event.chat.type == "private" and ((data.get("raw_state") and is_admin) or cmd in CLEAN_CMDS or pressed):
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
    """Короткий промт — текстом в <code>-блоке, длинный — текстовым файлом. Внизу всегда подпись автора."""
    if len(prompt) > FILE_THRESHOLD:
        # utf-8-sig: BOM помогает Блокноту/Windows правильно открыть кириллицу
        data = prompt.encode("utf-8-sig")
        await bot.send_document(
            user_id,
            BufferedInputFile(data, filename=f"prompt_{post_id}.txt"),
            caption=f"Ваш промт 💛\nОн длинный, поэтому прислан файлом — откройте и скопируйте текст.\n\n{SIGN}",
        )
        return
    await bot.send_message(user_id, "Ваш промт 💛")
    chunks = split_text(prompt)
    for i, chunk in enumerate(chunks):
        tail = f"\n\n{SIGN}" if i == len(chunks) - 1 else ""  # подпись под последней частью
        await bot.send_message(user_id, f"<code>{html.escape(chunk)}</code>{tail}", disable_web_page_preview=True)


async def send_tip(bot: Bot, user_id: int, post) -> None:
    """Совет под промтом: с фото публикации из канала (одиночка / пара)."""
    text = TIP_PAIR if post["kind"] == "pair" else TIP_SINGLE
    try:
        if post["photo"]:
            await bot.send_photo(user_id, post["photo"], caption=text)
        else:
            await bot.send_message(user_id, text)
    except Exception as e:
        log.warning("не удалось отправить совет: %s", e)
        try:
            await bot.send_message(user_id, text)
        except Exception:
            pass


async def announce_ready(bot: Bot, user_id: int, post_id: int) -> None:
    """Анимация загрузки в процентах, затем «Ваш промт готов» с кнопкой «Открыть» (мини-приложение)."""
    title = "⏳ Готовим ваш промт…"
    msg = await bot.send_message(user_id, f"{title}\n\n{bar(0)} 0%")
    _trash.setdefault(user_id, []).append(msg.message_id)
    for p in (14, 31, 47, 63, 78, 91, 100):
        await asyncio.sleep(0.5)
        try:
            await bot.edit_message_text(f"{title}\n\n{bar(p)} {p}%", chat_id=user_id, message_id=msg.message_id)
        except Exception:
            pass
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=f"{WEBAPP_URL}/app?p={post_id}"))
    ]])
    try:
        await bot.edit_message_text(
            "✅ <b>Ваш промт готов!</b>\n\nНажмите на кнопку «Открыть», чтобы получить его.",
            chat_id=user_id, message_id=msg.message_id, reply_markup=kb,
        )
    except Exception:
        sent = await bot.send_message(user_id, "✅ <b>Ваш промт готов!</b>\n\nНажмите на кнопку «Открыть», чтобы получить его.",
                                      reply_markup=kb)
        _trash.setdefault(user_id, []).append(sent.message_id)


def get_setting(key: str) -> str:
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else ""


def set_setting(key: str, value: str) -> None:
    with db() as c:
        c.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


async def send_saver_promo(bot: Bot, user_id: int) -> None:
    """Реклама бота-скачивателя: картинка + кнопка «Открыть» (если картинки нет — просто текст с кнопкой)."""
    global _saver_photo_id
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть", url=SAVER_URL)]])
    # приоритет: фото, выбранное админом из канала (/setpromo) → SAVER_PROMO_PHOTO → файл saver_promo.jpg
    photo = _saver_photo_id or get_setting("promo_photo") or SAVER_PROMO_PHOTO
    try:
        if photo and (_saver_photo_id or photo.startswith(("http", "AgAC")) or os.path.exists(photo)):
            src = photo if (_saver_photo_id or get_setting("promo_photo") == photo or photo.startswith(("http", "AgAC"))) else FSInputFile(photo)
            sent = await bot.send_photo(user_id, src, caption=SAVER_TEXT, reply_markup=kb)
            if sent.photo:
                _saver_photo_id = sent.photo[-1].file_id
            return
    except Exception as e:
        log.warning("не удалось отправить картинку рекламы: %s", e)
        _saver_photo_id = ""
    try:
        await bot.send_message(user_id, SAVER_TEXT, reply_markup=kb)
    except Exception:
        pass


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
    if WEBAPP_URL:
        await announce_ready(bot, user_id, post_id)  # промт откроется в мини-приложении
        await send_saver_promo(bot, user_id)  # реклама — сразу после «промт готов» (после скачивания её нет)
    else:
        await send_prompt(bot, user_id, post_id, post["prompt"])
        await send_tip(bot, user_id, post)
        await send_saver_promo(bot, user_id)
    ensure_user_row(user_id)
    if not FEATURES_LOCKED:  # пока функция «Создать свой промт» закрыта — это сообщение не отправляем
        await bot.send_message(user_id, promo_text(user_id), reply_markup=promo_kb())
    await ensure_reply_kb(bot, user_id)


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
        await reply(m, menu_text(m.from_user.id), reply_markup=main_kb())
        with db() as c:
            c.execute("UPDATE users SET kb_shown=1 WHERE user_id=?", (m.from_user.id,))


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


async def _vision_prompt(base: str, key: str, model: str, img: bytes, mime: str, extra: dict | None = None) -> str:
    import aiohttp

    data_url = f"data:{mime};base64,{base64.b64encode(img).decode()}"
    body = {
        "model": model,
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
    headers = {"Authorization": f"Bearer {key}", **(extra or {})}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as http:
            async with http.post(f"{base}/chat/completions", json=body, headers=headers) as r:
                text = await r.text()
                if r.status != 200:
                    raise _ai_error(r.status, text)
    except aiohttp.ClientError as e:
        raise AIError("😔 Нет связи с сервисом генерации. Попробуйте позже — генерация не списана.", str(e))
    msg = json.loads(text)["choices"][0]["message"]
    out = (msg.get("content") or "").strip()
    if msg.get("refusal") or len(out) < 80:
        raise AIError("😔 Не удалось разобрать эту картинку. Попробуйте другую.")
    return out


async def ai_make_prompt(img: bytes, mime: str) -> str:
    """Сначала бесплатный OpenRouter (если задан ключ), при любой неудаче — OpenAI."""
    if OPENROUTER_API_KEY:
        try:
            return await _vision_prompt(
                OPENROUTER_BASE_URL, OPENROUTER_API_KEY, OPENROUTER_MODEL, img, mime,
                {"HTTP-Referer": f"https://t.me/{BOT_USERNAME}", "X-Title": "Prompt bot"},
            )
        except Exception as e:
            log.warning("OpenRouter не справился (%s), пробуем OpenAI", e)
            if not OPENAI_API_KEY:
                raise
    return await _vision_prompt(OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_VISION_MODEL, img, mime)


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

    or_line = ""
    if OPENROUTER_API_KEY:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
                async with http.get(f"{OPENROUTER_BASE_URL}/key", headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"}) as r:
                    ok = r.status == 200
        except Exception:
            ok = False
        or_line = (f"OpenRouter (бесплатный промт): {'✅ ключ работает' if ok else '❌ ключ не подошёл'}, "
                   f"модель <code>{OPENROUTER_MODEL}</code>\n")
    if not OPENAI_API_KEY:
        return or_line + "❌ OPENAI_API_KEY не задан (без него не работает создание фото)."
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
        or_line + "✅ Ключ OpenAI работает.\n"
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


def ensure_user_row(uid: int) -> None:
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id, first_seen) VALUES(?,?)", (uid, int(time.time())))


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


# Тексты кнопок. Пока FEATURES_LOCKED=1 — вместо иконки стоит 🔒.
# Варианты и с замком, и без учитываются всегда, поэтому старые клавиатуры у пользователей тоже работают.
BTN_CREATE = "🔒 Создать свой промт" if FEATURES_LOCKED else "🎨 Создать свой промт"
BTN_SHARE = "📤 Поделиться"
BTN_BUY = "🔒 Купить генерации" if FEATURES_LOCKED else "💳 Купить генерации"
BTN_BAL = "🔒 Мой баланс" if FEATURES_LOCKED else "💎 Мой баланс"
CREATE_ALL = {"🎨 Создать свой промт", "🔒 Создать свой промт"}
BUY_ALL = {"💳 Купить генерации", "🔒 Купить генерации"}
BAL_ALL = {"💎 Мой баланс", "🔒 Мой баланс"}
BUTTON_TEXTS = {BTN_SHARE} | CREATE_ALL | BUY_ALL | BAL_ALL


def ico(normal: str) -> str:
    """Иконка для inline-кнопок: 🔒 при включённом замке."""
    return "🔒" if FEATURES_LOCKED else normal


def main_kb() -> ReplyKeyboardMarkup:
    """Постоянные кнопки внизу чата (не пропадают)."""
    app_row = [[KeyboardButton(text="📱 Открыть приложение", web_app=WebAppInfo(url=f"{WEBAPP_URL}/app"))]] if WEBAPP_URL else []
    return ReplyKeyboardMarkup(
        keyboard=app_row + [
            [KeyboardButton(text=BTN_CREATE)],
            [KeyboardButton(text=BTN_SHARE), KeyboardButton(text=BTN_BUY)],
            [KeyboardButton(text=BTN_BAL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие 👇",
    )


async def ensure_reply_kb(bot: Bot, uid: int) -> None:
    """Один раз показывает постоянные кнопки (например, тем, кто пришёл по ссылке из канала)."""
    with db() as c:
        row = c.execute("SELECT kb_shown FROM users WHERE user_id=?", (uid,)).fetchone()
        if row and row["kb_shown"]:
            return
        c.execute("UPDATE users SET kb_shown=1 WHERE user_id=?", (uid,))
    try:
        await bot.send_message(uid, "👇 Кнопки меню теперь всегда внизу — пользуйтесь ими в любой момент.",
                               reply_markup=main_kb())
    except Exception:
        pass


def promo_text(uid: int) -> str:
    free, _ = balance(uid)
    gift = (f"🎁 У вас уже есть <b>{free}</b> бесплатная(ых) генерация(й) — попробуйте прямо сейчас!"
            if free else f"🎁 Пригласите {REF_FRIENDS_NEEDED} друзей — получите бесплатную генерацию.")
    return (
        "✨ <b>Хотите такой же промт для своей картинки?</b>\n\n"
        "Отправьте мне любую картинку, которую хотите повторить, — я составлю для неё точный промт. "
        "Вставьте его в ChatGPT со своим фото и получите такой же кадр, но с вами. "
        "Можно повторять сколько угодно раз 🔁\n\n" + gift
    )


def promo_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{ico('🎨')} Создать свой промт", callback_data="gen:new")
    kb.button(text="📤 Поделиться с друзьями", callback_data="menu:share")
    kb.adjust(1)
    return kb.as_markup()


CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/tajpromt")


def menu_text(uid: int) -> str:
    if FEATURES_LOCKED:  # пока функции закрыты — приглашаем в канал и в приложение
        return (
            "👋 Привет!\n\n"
            f'Заходи в канал <a href="{CHANNEL_URL}">{CHANNEL_URL.replace("https://", "")}</a>, выбирай фото, '
            "нажми на кнопку и получи промт.\n\n"
            "Или открой приложение 👇"
        )
    free, paid = balance(uid)
    return (
        "👋 Привет!\n\nЯ создам <b>точный промт</b> по любой картинке. Отправьте этот промт в ChatGPT вместе со "
        "своим фото — и получите такую же картинку, но с вами.\n\n"
        f"🎁 Бесплатных генераций: <b>{free}</b>\n💎 Платных: <b>{paid}</b>\n\n"
        f"🎁 Пригласите {REF_FRIENDS_NEEDED} друзей — получите ещё одну бесплатную генерацию."
    )


def user_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{ico('🎨')} Создать свой промт", callback_data="gen:new")
    kb.button(text="📤 Поделиться", callback_data="menu:share")
    kb.button(text=f"{ico('💳')} Купить генерации", callback_data="buy:menu")
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
    await reply(m, menu_text(m.from_user.id), reply_markup=main_kb())
    with db() as c:
        c.execute("UPDATE users SET kb_shown=1 WHERE user_id=?", (m.from_user.id,))


@router.callback_query(F.data == "menu:home")
async def cb_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    ensure_user(cb.from_user)
    await edit_or_send(cb, menu_text(cb.from_user.id), user_menu())
    await cb.answer()


def share_content(uid: int):
    link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"
    with db() as c:
        invited = c.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (uid,)).fetchone()[0]
    share = (f"https://t.me/share/url?url={quote(link)}"
             f"&text={quote('Бот делает точный промт по любой картинке 🎨 Получи бесплатную генерацию!')}")
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Поделиться с друзьями", url=share)
    kb.button(text="⬅️ Меню", callback_data="menu:home")
    kb.adjust(1)
    text = (
        f"📤 <b>Пригласите друзей</b>\n\nВаша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено друзей: <b>{invited}</b>\n"
        f"За {REF_FRIENDS_NEEDED} друзей — 🎁 +1 бесплатная генерация. Друг тоже получает бесплатную генерацию."
    )
    return text, kb.as_markup()


@router.callback_query(F.data == "menu:share")
async def cb_share(cb: CallbackQuery):
    ensure_user(cb.from_user)
    text, kb = share_content(cb.from_user.id)
    await edit_or_send(cb, text, kb)
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


ASK_REF = (
    "📸 <b>Отправьте картинку, которую хотите повторить для своего фото.</b>\n\n"
    "Лучше хорошего качества — тогда промт получится точнее. Генерация спишется только после успешного результата."
)
CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu:home")]])


async def begin_create(user, state: FSMContext):
    """Общая логика кнопки «Создать свой промт». Возвращает (текст, клавиатура, предупреждение)."""
    ensure_user(user)
    await state.clear()
    free, paid = balance(user.id)
    if free + paid == 0:
        return buy_text("😔 Генерации закончились.\n\n"), buy_kb(), None
    if not (OPENAI_API_KEY or OPENROUTER_API_KEY):
        return None, None, "Сервис временно недоступен. Попробуйте позже."
    await state.set_state(Create.ref)
    return ASK_REF, CANCEL_KB, None


@router.callback_query(F.data == "gen:new")
async def cb_gen_new(cb: CallbackQuery, state: FSMContext):
    if FEATURES_LOCKED:
        return await cb.answer(LOCK_TEXT, show_alert=True)
    text, kb, alert = await begin_create(cb.from_user, state)
    if alert:
        return await cb.answer(alert, show_alert=True)
    await edit_or_send(cb, text, kb)
    await cb.answer()


# --- постоянные кнопки внизу чата (reply-клавиатура) ---

@router.message(F.text.in_(CREATE_ALL))
async def btn_create(m: Message, state: FSMContext):
    if FEATURES_LOCKED:
        return await m.answer(LOCK_TEXT)
    text, kb, alert = await begin_create(m.from_user, state)
    await m.answer(alert or text, reply_markup=kb)


@router.message(F.text == BTN_SHARE)
async def btn_share(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user)
    text, kb = share_content(m.from_user.id)
    await m.answer(text, reply_markup=kb)


@router.message(F.text.in_(BUY_ALL))
async def btn_buy(m: Message, state: FSMContext):
    if FEATURES_LOCKED:
        return await m.answer(LOCK_TEXT)
    await state.clear()
    ensure_user(m.from_user)
    await m.answer(buy_text(), reply_markup=buy_kb())


@router.message(F.text.in_(BAL_ALL))
async def btn_balance(m: Message, state: FSMContext):
    if FEATURES_LOCKED:
        return await m.answer(LOCK_TEXT)
    await state.clear()
    ensure_user(m.from_user)
    await m.answer(menu_text(m.from_user.id), reply_markup=user_menu())


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
            f"со своим фото, где хорошо видно лицо.\n\nОсталось: 🎁 {free} · 💎 {paid}\n\n{SIGN}"
        ),
        reply_markup=kb.as_markup(),
    )


@router.message(Create.ref)
async def ref_wrong(m: Message):
    await m.answer("Отправьте картинку (фото), которую хотите повторить 📸")


# ───────────────────────────── Генерации: «сгенерировать моё фото» (только платные) ─────────────────────────────

@router.callback_query(F.data.startswith("genphoto:"))
async def cb_genphoto(cb: CallbackQuery, state: FSMContext):
    if FEATURES_LOCKED:
        return await cb.answer(LOCK_TEXT, show_alert=True)
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
    if FEATURES_LOCKED:
        return await cb.answer(LOCK_TEXT, show_alert=True)
    await state.clear()
    ensure_user(cb.from_user)
    await edit_or_send(cb, buy_text(), buy_kb())
    await cb.answer()


@router.callback_query(F.data.regexp(r"^buy:\d+$"))
async def cb_buy(cb: CallbackQuery, state: FSMContext):
    if FEATURES_LOCKED:
        return await cb.answer(LOCK_TEXT, show_alert=True)
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
            kb.button(text=f"{ico('🎨')} Создать свой промт", callback_data="gen:new")
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


ASK_PROMO = ("🖼 Перешлите сюда <b>пост из вашего канала с фото</b> (или просто отправьте фото) — "
             "оно станет картинкой рекламы Taj_Saverbot.\nОтмена: /cancel")


@router.callback_query(F.data == "adm:setpromo", ADMIN)
async def cb_setpromo(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetPromo.wait)
    await cb.message.edit_text(ASK_PROMO)
    await cb.answer()


@router.message(Command("setpromo"), ADMIN)
async def cmd_setpromo(m: Message, state: FSMContext):
    await state.set_state(SetPromo.wait)
    await reply(m, ASK_PROMO)


@router.message(SetPromo.wait, ADMIN, F.photo)
async def got_promo_photo(m: Message, state: FSMContext, bot: Bot):
    global _saver_photo_id
    set_setting("promo_photo", m.photo[-1].file_id)
    _saver_photo_id = ""
    await state.clear()
    await wipe(bot, m.chat.id)
    await reply(m, "✅ Фото рекламы сохранено. Вот как его увидят пользователи:")
    await send_saver_promo(bot, m.from_user.id)
    await reply(m, "Меню:", reply_markup=admin_menu())


@router.message(SetPromo.wait, ADMIN, ~F.text.startswith("/"))
async def promo_wrong(m: Message):
    await reply(m, "Нужен пост с фото или просто фото. Отмена: /cancel")


@router.callback_query(F.data == "adm:stats", ADMIN)
async def cb_stats(cb: CallbackQuery):
    now = int(time.time())
    with db() as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        d1 = c.execute("SELECT COUNT(*) FROM users WHERE first_seen>=?", (now - 86400,)).fetchone()[0]
        d7 = c.execute("SELECT COUNT(*) FROM users WHERE first_seen>=?", (now - 7 * 86400,)).fetchone()[0]
        refs = c.execute("SELECT COUNT(*) FROM users WHERE ref_by IS NOT NULL").fetchone()[0]
        webapp = c.execute("SELECT COUNT(*) FROM users WHERE webapp_at IS NOT NULL").fetchone()[0]
        posts = c.execute("SELECT COUNT(*), COALESCE(SUM(kind='pair'),0) FROM posts").fetchone()
        claims = c.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        uniq = c.execute("SELECT COUNT(DISTINCT user_id) FROM claims").fetchone()[0]
        gens = c.execute("SELECT COUNT(*), COALESCE(SUM(photo_used),0) FROM gens").fetchone()
        pays = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE status='ok'").fetchone()
        pend = c.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Список пользователей (файл)", callback_data="adm:users")
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    kb.adjust(1)
    await cb.message.edit_text(
        f"📊 <b>Статистика</b>\n\n👥 <b>Пользователей бота: {users}</b>\n"
        f"Новых за 24 часа: {d1}, за 7 дней: {d7}\nПришли по приглашению друзей: {refs}\n"
        f"Открывали мини-приложение: {webapp}\n\n"
        f"Публикаций: {posts[0]} (одиночка: {posts[0] - posts[1]}, пара: {posts[1]})\n"
        f"Выдач промта: {claims} (уникальных людей: {uniq})\n\n"
        f"🎨 Создано промтов: {gens[0]}, фото сгенерировано: {gens[1]}\n"
        f"💳 Платежей подтверждено: {pays[0]} на {fmt_price(pays[1])} сомони, ждут решения: {pend}",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data == "adm:users", ADMIN)
async def cb_users(cb: CallbackQuery, bot: Bot):
    with db() as c:
        rows = c.execute("SELECT user_id, username, first_seen FROM users ORDER BY first_seen DESC").fetchall()
    lines = [f"Всего: {len(rows)}", "id | username | дата"]
    for r in rows:
        who = f"@{r['username']}" if r["username"] else "-"
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["first_seen"] or 0))
        lines.append(f"{r['user_id']} | {who} | {when} UTC")
    await bot.send_document(cb.from_user.id, BufferedInputFile("\n".join(lines).encode("utf-8-sig"), filename="users.txt"),
                            caption=f"👥 Пользователей: {len(rows)}")
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
        "1/5. Пришлите <b>текст публикации</b> для канала (можно с фото и форматированием).\n"
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
        "2/5. Пришлите <b>промт для этой публикации</b> обычным текстом (без фото) "
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
    await state.set_state(NewPost.kind)
    kb = InlineKeyboardBuilder()
    kb.button(text="🧍 Одиночка", callback_data="pk:single")
    kb.button(text="👫 Пара", callback_data="pk:pair")
    kb.adjust(2)
    await reply(m, "3/5. Это фото <b>одиночное</b> или <b>парное</b>? От этого зависит совет, "
                   "который получит пользователь после промта.", reply_markup=kb.as_markup())


@router.callback_query(NewPost.kind, F.data.startswith("pk:"), ADMIN)
async def np_kind(cb: CallbackQuery, state: FSMContext):
    await state.update_data(kind=cb.data.split(":")[1])
    with db() as c:
        catalog = c.execute("SELECT * FROM subs ORDER BY id").fetchall()
    await cb.answer()
    m = cb.message
    if not catalog:
        await state.set_state(NewPost.channel)
        return await edit_or_send(cb,
            "4/5. В каталоге нет каналов/ботов для подписки — эта публикация будет без подписки "
            "(добавить их можно в меню «Каналы и боты»).\n\n"
            "5/5. Куда публиковать? Пришлите <code>@username</code> канала, ID или перешлите сообщение из канала."
        )
    await state.set_state(NewPost.subs)
    await edit_or_send(cb,
        "4/5. На что нужно подписаться <b>для этой публикации</b>? Отметьте и нажмите «Далее» "
        "(можно не выбирать ничего).",
        picker_kb(catalog, set()),
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
        "5/5. Куда публиковать? Пришлите <code>@username</code> канала или числовой ID, "
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
            "INSERT INTO posts(text, photo, prompt, channel_id, created, kind) VALUES(?,?,?,?,?,?)",
            (data.get("text") or "", data.get("photo"), data["prompt"], str(chat_ref), int(time.time()),
             data.get("kind") or "single"),
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

# ───────────────────────────── Мини-приложение: сервер и API ─────────────────────────────

def check_init_data(init_data: str):
    """Проверка подписи Telegram (initData). Возвращает dict пользователя или None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        if not got or not BOT_TOKEN:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user if user.get("id") else None
    except Exception:
        return None


def webapp_register(u: dict) -> None:
    """Регистрирует пользователя мини-приложения и привязывает к профилю (id, username, имя)."""
    now = int(time.time())
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id, username, first_seen) VALUES(?,?,?)",
                  (u["id"], u.get("username"), now))
        c.execute("UPDATE users SET username=COALESCE(?, username), first_name=?, "
                  "webapp_at=COALESCE(webapp_at, ?) WHERE user_id=?",
                  (u.get("username"), u.get("first_name"), now, u["id"]))


_photo_cache: dict[str, bytes] = {}
_dl_last: dict[int, float] = {}


async def _wa_auth(request):
    """Проверка запроса мини-приложения. Возвращает (user, body, ошибка-ответ); пользователь сразу регистрируется."""
    from aiohttp import web

    try:
        body = await request.json()
    except Exception:
        return None, {}, web.json_response({"ok": False, "error": "bad_request"}, status=400)
    user = check_init_data(str(body.get("initData", "")))
    if not user:
        return None, body, web.json_response({"ok": False, "error": "bad_init"}, status=401)
    webapp_register(user)
    return user, body, None


def _wa_post(body):
    try:
        pid = int(body.get("post", 0))
    except (TypeError, ValueError):
        pid = 0
    with db() as c:
        return c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()


def _has_claim(uid: int, pid: int) -> bool:
    with db() as c:
        return bool(c.execute("SELECT 1 FROM claims WHERE user_id=? AND post_id=?", (uid, pid)).fetchone())


def make_web_app(bot: Bot):
    from aiohttp import web

    def jerr(code: str, status: int):
        return web.json_response({"ok": False, "error": code}, status=status)

    async def page(request):
        try:
            with open(WEBAPP_HTML, encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="text/html", headers={"Cache-Control": "no-store"})
        except OSError:
            return web.Response(text="webapp.html не найден рядом с bot.py", status=500)

    async def api_profile(request):
        user, body, err = await _wa_auth(request)
        if err:
            return err
        uid = user["id"]
        with db() as c:
            invited = c.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (uid,)).fetchone()[0]
            got = c.execute("SELECT COUNT(DISTINCT post_id) FROM claims WHERE user_id=?", (uid,)).fetchone()[0]
        return web.json_response({"ok": True, "invited": invited, "got": got,
                                  "link": f"https://t.me/{BOT_USERNAME}?start=ref{uid}"})

    async def api_list(request):
        user, body, err = await _wa_auth(request)
        if err:
            return err
        only_fav = bool(body.get("fav"))
        sql = ("SELECT p.id, p.kind, p.created, (p.photo IS NOT NULL AND p.photo != '') AS hp, "
               "EXISTS(SELECT 1 FROM favorites f WHERE f.user_id=? AND f.post_id=p.id) AS fav FROM posts p ")
        if only_fav:
            sql += "WHERE EXISTS(SELECT 1 FROM favorites f WHERE f.user_id=? AND f.post_id=p.id) "
        sql += "ORDER BY p.id DESC LIMIT 60"
        with db() as c:
            rows = c.execute(sql, (user["id"], user["id"]) if only_fav else (user["id"],)).fetchall()
        items = [{"id": r["id"], "kind": r["kind"] or "single", "created": r["created"] or 0,
                  "has_photo": bool(r["hp"]), "fav": bool(r["fav"])} for r in rows]
        return web.json_response({"ok": True, "items": items})

    async def api_post(request):
        """Открыть промт: сразу, если уже получен; иначе проверяем подписки этой публикации."""
        user, body, err = await _wa_auth(request)
        if err:
            return err
        post = _wa_post(body)
        if not post:
            return jerr("no_post", 404)
        uid, pid = user["id"], post["id"]
        if not _has_claim(uid, pid):
            missing = await missing_channels(bot, uid, pid)
            not_started, manual = await check_bots(bot, uid, pid)
            need = missing + not_started + ([] if body.get("confirm") else manual)
            if need:
                return web.json_response({"ok": False, "error": "need_subs", "subs": [
                    {"title": x["title"], "link": x["link"], "kind": x["kind"]} for x in need]}, status=403)
            with db() as c:
                c.execute("INSERT INTO claims(user_id, post_id, ts) VALUES(?,?,?)", (uid, pid, int(time.time())))
        with db() as c:
            fav = bool(c.execute("SELECT 1 FROM favorites WHERE user_id=? AND post_id=?", (uid, pid)).fetchone())
        return web.json_response({"ok": True, "kind": post["kind"] or "single", "prompt": post["prompt"],
                                  "has_photo": bool(post["photo"]), "fav": fav})

    async def api_fav(request):
        user, body, err = await _wa_auth(request)
        if err:
            return err
        post = _wa_post(body)
        if not post:
            return jerr("no_post", 404)
        on = bool(body.get("on"))
        with db() as c:
            if on:
                c.execute("INSERT OR IGNORE INTO favorites(user_id, post_id, ts) VALUES(?,?,?)",
                          (user["id"], post["id"], int(time.time())))
            else:
                c.execute("DELETE FROM favorites WHERE user_id=? AND post_id=?", (user["id"], post["id"]))
        return web.json_response({"ok": True, "fav": on})

    async def api_photo(request):
        # GET: фото публикации (оно и так публичное в канале), но только для запросов из мини-приложения
        if not check_init_data(request.query.get("d", "")):
            return web.Response(status=401)
        try:
            pid = int(request.query.get("post", "0"))
        except ValueError:
            return web.Response(status=404)
        with db() as c:
            row = c.execute("SELECT photo FROM posts WHERE id=?", (pid,)).fetchone()
        if not row or not row["photo"]:
            return web.Response(status=404)
        fid = row["photo"]
        if fid not in _photo_cache:
            buf = await bot.download(fid)
            if len(_photo_cache) > 120:
                _photo_cache.clear()
            _photo_cache[fid] = buf.getvalue()
        return web.Response(body=_photo_cache[fid], content_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

    async def api_download(request):
        user, body, err = await _wa_auth(request)
        if err:
            return err
        post = _wa_post(body)
        if not post:
            return jerr("no_post", 404)
        uid = user["id"]
        if not _has_claim(uid, post["id"]):
            return jerr("no_claim", 403)
        if time.time() - _dl_last.get(uid, 0) < 4:
            return web.json_response({"ok": True})  # защита от двойного нажатия
        _dl_last[uid] = time.time()
        try:
            await bot.send_document(
                uid,
                BufferedInputFile(post["prompt"].encode("utf-8-sig"), filename=f"prompt_{post['id']}.txt"),
                caption=f"Ваш промт 💛\nОткройте файл и скопируйте текст.\n\n{SIGN}",
            )
        except Exception as e:
            log.warning("отправка промта из мини-приложения: %s", e)
            return jerr("send_failed", 500)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/app", page)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    app.router.add_get("/api/photo", api_photo)
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/list", api_list)
    app.router.add_post("/api/post", api_post)
    app.router.add_post("/api/open", api_post)  # старое имя
    app.router.add_post("/api/fav", api_fav)
    app.router.add_post("/api/download", api_download)
    return app


# ═══════════════════ ВТОРОЙ БОТ: публикации в своих каналах ═══════════════════
# Включается переменной PUBLISH_BOT_TOKEN (токен второго бота). Работает в этом же процессе
# рядом с первым ботом (BOT_TOKEN). Таблицы в базе начинаются с pub_ — с первым ботом не пересекаются.

PUB_BOT_TOKEN = os.getenv("PUBLISH_BOT_TOKEN", "")
PUB_ADMIN_IDS = {int(x) for x in os.getenv("PUB_ADMIN_IDS", "").replace(" ", "").split(",") if x} or ADMIN_IDS
PUB_TZ = int(os.getenv("PUB_TZ_OFFSET", "5"))  # часовой пояс для «публикаций в день» (Таджикистан = UTC+5)
PUB_PAY_LINES = [("DC city", os.getenv("PUB_PAY_DC", "978117811")),
                 ("Alif mobi", os.getenv("PUB_PAY_ALIF", "978117811"))]
PUB_DAYS = 30
PUB_PLANS = {
    "free": {"name": "FREE", "icon": "🆓", "price": 0, "channels": 1, "daily": 1},
    "standard": {"name": "STANDART", "icon": "⭐", "price": 29, "channels": 3, "daily": 10},
    "pro": {"name": "PRO", "icon": "👑", "price": 59, "channels": 5, "daily": None},
}
PUB_MAX_BUTTONS = 8
PUB_CUSTOM_BOT_PLANS = {"pro"}  # на каких тарифах можно подключить свой бот
PUB_REF_BONUS = int(os.getenv("PUB_REF_BONUS", "2"))  # сколько публикаций получает пригласивший за каждого друга
PUB_BTN_NEW = "📝 Новая публикация"
PUB_BTN_EDIT = "✏️ Редактировать публикации"
PUB_BTN_PROFILE = "👤 Мой профиль"
PUB_BTN_PLANS = "💎 Подписки"
PUB_BTN_INVITE = "🎁 Пригласить друга"
PUB_MENU_TEXTS = {PUB_BTN_NEW, PUB_BTN_EDIT, PUB_BTN_PROFILE, PUB_BTN_PLANS, PUB_BTN_INVITE}
PUB_ACTIONS = {  # действия кнопки под постом
    "file": "📁 Отправить файл (фото, видео, музыка...)",
    "link": "🔗 Открыть ссылку",
    "text": "💬 Прислать текст в личку",
    "dm": "📩 Переслать текст в личку админу",
    "alert": "🔔 Всплывающее окно",
    "like": "👍 Лайк со счётчиком",
}
PUB_COLORS = {  # цвет кнопки (Telegram: style) — доступно на STANDART и PRO
    "primary": "🔵 Синяя",
    "success": "🟢 Зелёная",
    "danger": "🔴 Красная",
    "none": "⚪ Обычная",
}
PUB_SEND_KINDS = ("photo", "video", "audio", "voice", "animation", "document")

pub_router = Router()
PUB_ADMIN = F.from_user.id.in_(PUB_ADMIN_IDS)
_pub_username = ""
_pub_pay_msgs: dict[int, list] = {}
_custom: dict[int, dict] = {}  # запущенные личные боты пользователей: id -> {bot, task, owner, username}


class PubNew(StatesGroup):
    content = State()
    caption = State()
    confirm = State()
    btn_label = State()
    btn_action = State()
    btn_value = State()
    btn_color = State()
    btn_cond = State()
    btn_need = State()
    btn_layout = State()


class PubChan(StatesGroup):
    wait = State()


class PubPay(StatesGroup):
    receipt = State()


class PubReject(StatesGroup):
    reason = State()


class PubEdit(StatesGroup):
    caption = State()


class PubBot(StatesGroup):
    token = State()


class PubError(Exception):
    pass


# ── база ──

def pub_init_db() -> None:
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS pub_users(
                user_id INTEGER PRIMARY KEY, username TEXT,
                plan TEXT DEFAULT 'free', plan_until INTEGER DEFAULT 0, created INTEGER
            );
            CREATE TABLE IF NOT EXISTS pub_channels(
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER,
                title TEXT, username TEXT, UNIQUE(owner_id, chat_id)
            );
            CREATE TABLE IF NOT EXISTS pub_posts(
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER, title TEXT,
                message_id INTEGER, kind TEXT, file_id TEXT, text TEXT, created INTEGER
            );
            CREATE TABLE IF NOT EXISTS pub_buttons(
                id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER, label TEXT, action TEXT,
                payload TEXT, file_kind TEXT, extra TEXT, clicks INTEGER DEFAULT 0, style TEXT,
                same_row INTEGER DEFAULT 0, need_chat INTEGER, need_title TEXT, need_link TEXT
            );
            CREATE TABLE IF NOT EXISTS pub_likes(
                button_id INTEGER, user_id INTEGER, PRIMARY KEY(button_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS pub_bots(
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER UNIQUE, token TEXT,
                username TEXT, tg_id INTEGER UNIQUE, status TEXT DEFAULT 'active', created INTEGER
            );
            CREATE TABLE IF NOT EXISTS pub_payments(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, plan TEXT,
                amount INTEGER, receipt TEXT, receipt_kind TEXT, status TEXT DEFAULT 'pending',
                reason TEXT, wait_mid INTEGER, created INTEGER
            );
            """
        )
        have = [r[1] for r in c.execute("PRAGMA table_info(pub_buttons)")]
        for name, ddl in (("style", "TEXT"), ("same_row", "INTEGER DEFAULT 0"), ("need_chat", "INTEGER"),
                          ("need_title", "TEXT"), ("need_link", "TEXT")):  # для старых баз
            if name not in have:
                c.execute(f"ALTER TABLE pub_buttons ADD COLUMN {name} {ddl}")
        for tbl, name, ddl in (("pub_users", "ref_by", "INTEGER"), ("pub_users", "bonus_posts", "INTEGER DEFAULT 0"),
                               ("pub_users", "ref_count", "INTEGER DEFAULT 0"),
                               ("pub_channels", "bot_id", "INTEGER DEFAULT 0"), ("pub_posts", "bot_id", "INTEGER DEFAULT 0")):
            if name not in [r[1] for r in c.execute(f"PRAGMA table_info({tbl})")]:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {name} {ddl}")


def pub_user(u) -> None:
    with db() as c:
        c.execute("INSERT OR IGNORE INTO pub_users(user_id, username, created) VALUES(?,?,?)",
                  (u.id, u.username, int(time.time())))
        c.execute("UPDATE pub_users SET username=? WHERE user_id=?", (u.username, u.id))


def pub_plan_key(uid: int) -> str:
    """Текущий тариф. Когда месяц закончился — автоматически FREE. Админы бота — всегда PRO."""
    if uid in PUB_ADMIN_IDS:
        return "pro"
    with db() as c:
        r = c.execute("SELECT plan, plan_until FROM pub_users WHERE user_id=?", (uid,)).fetchone()
        if not r or r["plan"] not in PUB_PLANS or r["plan"] == "free":
            return "free"
        if (r["plan_until"] or 0) < time.time():
            c.execute("UPDATE pub_users SET plan='free', plan_until=0 WHERE user_id=?", (uid,))
            return "free"
        return r["plan"]


def pub_plan_until(uid: int) -> int:
    with db() as c:
        r = c.execute("SELECT plan_until FROM pub_users WHERE user_id=?", (uid,)).fetchone()
    return (r["plan_until"] or 0) if r else 0


def pub_date(ts: int) -> str:
    return time.strftime("%d.%m.%Y", time.gmtime(ts + PUB_TZ * 3600))


def pub_channels(uid: int) -> list:
    with db() as c:
        return c.execute("SELECT * FROM pub_channels WHERE owner_id=? ORDER BY id", (uid,)).fetchall()


def pub_today_count(uid: int) -> int:
    local = int(time.time()) + PUB_TZ * 3600
    start = local - local % 86400 - PUB_TZ * 3600
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM pub_posts WHERE owner_id=? AND created>=?", (uid, start)).fetchone()[0]


def pub_bonus(uid: int) -> int:
    with db() as c:
        r = c.execute("SELECT bonus_posts FROM pub_users WHERE user_id=?", (uid,)).fetchone()
    return (r["bonus_posts"] or 0) if r else 0


def pub_daily_blocked(uid: int) -> bool:
    """Дневной лимит исчерпан и бонусных публикаций нет."""
    plan = PUB_PLANS[pub_plan_key(uid)]
    return plan["daily"] is not None and pub_today_count(uid) >= plan["daily"] and pub_bonus(uid) <= 0


def pub_take_bonus(uid: int) -> bool:
    """Если дневной лимит уже исчерпан — тратит одну бонусную публикацию (True)."""
    plan = PUB_PLANS[pub_plan_key(uid)]
    if plan["daily"] is not None and pub_today_count(uid) >= plan["daily"]:
        with db() as c:
            return bool(c.execute("UPDATE pub_users SET bonus_posts=bonus_posts-1 WHERE user_id=? AND bonus_posts>0",
                                  (uid,)).rowcount)
    return False


def pub_limit_error(uid: int, chat_id: int) -> str:
    """Пустая строка — можно публиковать; иначе короткий текст причины."""
    key = pub_plan_key(uid)
    plan = PUB_PLANS[key]
    ch = next((r for r in pub_channels(uid)[:plan["channels"]] if r["chat_id"] == chat_id), None)
    if ch is None:
        return f"Этот канал недоступен на тарифе {plan['name']}."
    if ch["bot_id"] and key not in PUB_CUSTOM_BOT_PLANS:
        return "Публикации через свой бот — только на тарифе PRO."
    if pub_daily_blocked(uid):
        return f"Лимит на сегодня исчерпан ({plan['daily']}). Пригласите друга — +{PUB_REF_BONUS} публикации."
    return ""


def pub_post_items(post_id: int) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM pub_likes l WHERE l.button_id=b.id) AS likes, "
            "(SELECT text FROM pub_posts p WHERE p.id=b.post_id) AS post_text "
            "FROM pub_buttons b WHERE b.post_id=? ORDER BY b.id", (post_id,)).fetchall()
    return [dict(r) for r in rows]


# ── разметка и отправка ──

def pub_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PUB_BTN_NEW)],
                  [KeyboardButton(text=PUB_BTN_EDIT)],
                  [KeyboardButton(text=PUB_BTN_PROFILE), KeyboardButton(text=PUB_BTN_PLANS)],
                  [KeyboardButton(text=PUB_BTN_INVITE)]],
        resize_keyboard=True, is_persistent=True)


def pub_group_rows(items: list[dict]) -> list[list[dict]]:
    """Раскладка по рядам: кнопка с same_row идёт рядом с предыдущей (максимум 2 в ряду)."""
    rows: list[list[dict]] = []
    for b in items:
        if b.get("same_row") and rows and len(rows[-1]) < 2:
            rows[-1].append(b)
        else:
            rows.append([b])
    return rows


def pub_dm_url(username: str, post_text: str | None) -> str:
    """Ссылка на чат с админом: в поле ввода уже готово сообщение «хочу узнать подробнее» + текст поста."""
    plain = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", post_text or ""))).strip()
    base = f"https://t.me/{(username or '').lstrip('@')}?text="
    msg = "Здравствуйте! Хочу узнать об этом подробнее."
    for limit in (300, 200, 120, 60, 0):  # ссылка у Telegram ограничена по длине — укорачиваем текст
        part = plain[:limit].rstrip() + ("…" if len(plain) > limit and limit else "") if limit else ""
        url = base + quote(msg + (f"\n\n«{part}»" if part else ""))
        if len(url) <= 1800:
            return url
    return base + quote(msg)


def pub_make_btn(b: dict) -> InlineKeyboardButton:
    label = b["label"]
    if b["action"] == "dm":
        return InlineKeyboardButton(text=label, url=pub_dm_url(b["payload"], b.get("post_text")), style=b.get("style") or None)
    if b["action"] == "link":
        return InlineKeyboardButton(text=label, url=b["payload"], style=b.get("style") or None)
    if b["action"] == "like" and b.get("likes"):
        label = f"{label} {b['likes']}"
    return InlineKeyboardButton(text=label, callback_data=f"pb:{b['id']}" if b.get("id") else "pv",
                                style=b.get("style") or None)


def pub_build_markup(items: list[dict]):
    rows = [[pub_make_btn(b) for b in row] for row in pub_group_rows(items)]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def pub_insert_button(c, pid: int, b: dict) -> None:
    c.execute(
        "INSERT INTO pub_buttons(post_id, label, action, payload, file_kind, extra, style, same_row, need_chat, need_title, need_link) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, b["label"], b["action"], b["payload"], b.get("file_kind"), b.get("extra"), b.get("style"),
         b.get("same_row") or 0, b.get("need_chat"), b.get("need_title"), b.get("need_link")))


async def pub_check_sub(bot: Bot, uid: int, b: dict):
    """Проверка подписки для кнопки. Возвращает (можно_ли, текст_для_всплывающего_окна)."""
    if not b.get("need_chat"):
        return True, ""
    try:
        mem = await bot.get_chat_member(b["need_chat"], uid)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False, "Не удалось проверить подписку. Попробуйте позже."
    ok = mem.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR) or (
        mem.status == ChatMemberStatus.RESTRICTED and getattr(mem, "is_member", False))
    if ok:
        return True, ""
    link = f" {b['need_link']}" if b.get("need_link") else ""
    return False, f"🔒 Сначала подпишитесь на «{(b.get('need_title') or 'канал')[:60]}»{link}, затем нажмите кнопку ещё раз."[:200]


def pub_media(m: Message):
    if m.photo:
        return "photo", m.photo[-1].file_id
    if m.video:
        return "video", m.video.file_id
    if m.audio:
        return "audio", m.audio.file_id
    if m.voice:
        return "voice", m.voice.file_id
    if m.animation:
        return "animation", m.animation.file_id
    if m.document:
        return "document", m.document.file_id
    return None, None


def pub_html(m: Message) -> str:
    return m.html_text if (m.text or m.caption) else ""


async def pub_send(bot: Bot, chat_id: int, p: dict, markup=None, caption_override=None):
    """Отправляет пост (медиа с подписью или просто текст) и возвращает отправленное сообщение."""
    text = p.get("text") or ""
    if p["kind"] == "text":
        return await bot.send_message(chat_id, text, reply_markup=markup)
    return await getattr(bot, "send_" + p["kind"])(
        chat_id, **{p["kind"]: p["file_id"]}, caption=text or None, reply_markup=markup)


async def pub_deliver(bot: Bot, uid: int, b: dict) -> bool:
    """Выдаёт пользователю содержимое кнопки (файл или текст) в личные сообщения."""
    try:
        if b["action"] == "text":
            await bot.send_message(uid, b["payload"])
        else:
            kind = b["file_kind"] if b["file_kind"] in PUB_SEND_KINDS else "document"
            await getattr(bot, "send_" + kind)(uid, **{kind: b["payload"]}, caption=b.get("extra") or None)
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        return False


def pub_post_link(chat_id: int, username: str | None, mid: int) -> str:
    if username:
        return f"https://t.me/{username}/{mid}"
    return f"https://t.me/c/{str(chat_id).replace('-100', '', 1)}/{mid}"


def pub_plans_text(uid: int) -> str:
    cur = pub_plan_key(uid)
    lines = ["💎 <b>Тарифы</b>\n"]
    for key, p in PUB_PLANS.items():
        price = "бесплатно" if not p["price"] else f"{p['price']} сомони / месяц"
        daily = "безлимитные публикации" if p["daily"] is None else f"{p['daily']} публ. в день"
        colors = ("\n   🎨 цвета кнопок" if key != "free" else "") + ("\n   🤖 свой бот для файлов" if key == "pro" else "")
        lines.append(f"{p['icon']} <b>{p['name']}</b> — {price}\n   до {p['channels']} кан. • {daily}{colors}")
    until = pub_plan_until(uid)
    now = PUB_PLANS[cur]["name"] + (f" (до {pub_date(until)})" if cur != "free" and until else "")
    lines.append(f"\nВаш тариф: <b>{now}</b>")
    return "\n".join(lines)


def pub_plans_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key in ("standard", "pro"):
        p = PUB_PLANS[key]
        kb.button(text=f"{p['icon']} Подключить {p['name']} — {p['price']} сом.", callback_data=f"pp:{key}")
    kb.adjust(1)
    return kb.as_markup()


def pub_add_channel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить канал", callback_data="pc:add")]])


async def pub_ack(cb: CallbackQuery, *a, **k) -> None:
    try:
        await cb.answer(*a, **k)
    except TelegramBadRequest:
        pass


async def pub_safe_delete(bot: Bot, chat_id: int, mid: int) -> None:
    try:
        await bot.delete_message(chat_id, mid)
    except Exception:
        pass


# ── старт, меню ──

@pub_router.message(CommandStart())
async def pub_start(m: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    with db() as c:
        is_new = not c.execute("SELECT 1 FROM pub_users WHERE user_id=?", (m.from_user.id,)).fetchone()
    pub_user(m.from_user)
    arg = (command.args or "").strip()
    if arg.startswith("r") and arg[1:].isdigit() and is_new:  # пришёл по ссылке друга
        await pub_register_ref(bot, m.from_user.id, int(arg[1:]))
    if await pub_deep_deliver(m, bot, arg):  # пользователь нажал кнопку в канале, но бот ещё не был запущен
        return
    await m.answer(
        "👋 <b>Публикации в ваших каналах</b>\n\n"
        "1️⃣ Добавьте свой канал\n2️⃣ Создайте пост с фото, видео или музыкой\n"
        "3️⃣ Добавьте кнопку: файл, ссылка, текст и другое\n\nВсё управление — в кнопках внизу 👇",
        reply_markup=pub_main_kb())
    if not pub_channels(m.from_user.id):
        await m.answer("Начните с добавления канала:", reply_markup=pub_add_channel_kb())


@pub_router.message(Command("cancel"))
async def pub_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=pub_main_kb())


@pub_router.message(F.text == PUB_BTN_PROFILE)
async def pub_profile(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    pub_user(m.from_user)
    key = pub_plan_key(uid)
    plan = PUB_PLANS[key]
    until = pub_plan_until(uid)
    chans = pub_channels(uid)
    daily = "∞" if plan["daily"] is None else f"{pub_today_count(uid)}/{plan['daily']}"
    text = (f"👤 <b>Мой профиль</b>\n\nID: <code>{uid}</code>\n"
            f"Тариф: {plan['icon']} <b>{plan['name']}</b>"
            + (f" — до {pub_date(until)}" if key != "free" and until else "") + "\n"
            f"Каналы: {len(chans)}/{plan['channels']}\nПубликации сегодня: {daily}\n"
            f"🎁 Бонусные публикации: {pub_bonus(uid)}\n")
    kb = InlineKeyboardBuilder()
    for ch in chans:
        text += f"\n{'🤖' if ch['bot_id'] else '📢'} {html.escape(ch['title'] or str(ch['chat_id']))}"
        kb.button(text=f"🗑 Убрать: {(ch['title'] or '')[:25]}", callback_data=f"pc:del:{ch['id']}")
    kb.button(text="➕ Добавить канал", callback_data="pc:add")
    crow = pub_custom_row(uid)
    if crow:
        text += f"\n\n🤖 Свой бот: @{html.escape(crow['username'] or '')}"
        kb.button(text="🔄 Перевести каналы на свой бот", callback_data="cbot:move")
        kb.button(text="❌ Отключить свой бот", callback_data="cbot:off")
    else:
        kb.button(text="🤖 Подключить свой бот (PRO)", callback_data="cbot:add")
    kb.button(text="🎁 Пригласить друга", callback_data="ref:open")
    kb.button(text="💎 Тарифы", callback_data="pl:open")
    kb.adjust(1)
    await m.answer(text, reply_markup=kb.as_markup())


@pub_router.message(F.text == PUB_BTN_PLANS)
async def pub_plans(m: Message, state: FSMContext):
    await state.clear()
    pub_user(m.from_user)
    await m.answer(pub_plans_text(m.from_user.id), reply_markup=pub_plans_kb())


@pub_router.callback_query(F.data == "pl:open")
async def pub_plans_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer(pub_plans_text(cb.from_user.id), reply_markup=pub_plans_kb())
    await cb.answer()


# ── пригласить друга (бонусные публикации) ──

def pub_limit_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🎁 Пригласить друга (+{PUB_REF_BONUS} публикации)", callback_data="ref:open")
    for key in ("standard", "pro"):
        p = PUB_PLANS[key]
        kb.button(text=f"{p['icon']} Подключить {p['name']} — {p['price']} сом.", callback_data=f"pp:{key}")
    kb.adjust(1)
    return kb.as_markup()


async def pub_register_ref(bot: Bot, new_uid: int, inviter: int) -> None:
    """Новый пользователь пришёл по ссылке друга: пригласившему +PUB_REF_BONUS публикаций (один раз за человека)."""
    if inviter == new_uid:
        return
    with db() as c:
        if not c.execute("SELECT 1 FROM pub_users WHERE user_id=?", (inviter,)).fetchone():
            return
        done = c.execute("UPDATE pub_users SET ref_by=? WHERE user_id=? AND ref_by IS NULL", (inviter, new_uid)).rowcount
        if not done:
            return
        c.execute("UPDATE pub_users SET bonus_posts=COALESCE(bonus_posts,0)+?, ref_count=COALESCE(ref_count,0)+1 WHERE user_id=?",
                  (PUB_REF_BONUS, inviter))
    try:
        await bot.send_message(inviter, f"🎉 По вашей ссылке пришёл новый пользователь!\n"
                                        f"Вам начислено <b>+{PUB_REF_BONUS}</b> бесплатных публикаций. Баланс: {pub_bonus(inviter)}")
    except Exception:
        pass


async def pub_send_invite(target: Message, uid: int) -> None:
    link = f"https://t.me/{_pub_username}?start=r{uid}"
    with db() as c:
        r = c.execute("SELECT ref_count FROM pub_users WHERE user_id=?", (uid,)).fetchone()
    share = ("https://t.me/share/url?url=" + quote(link) +
             "&text=" + quote("Публикуй посты с кнопками и файлами в своём канале 👇"))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share)]])
    await target.answer(
        f"🎁 <b>Пригласи друга — получи +{PUB_REF_BONUS} публикации</b>\n\n"
        f"За каждого нового человека, который нажмёт «Старт» по вашей ссылке, вы получаете {PUB_REF_BONUS} бесплатных "
        "публикации. Они тратятся, когда дневной лимит тарифа закончился.\n\n"
        f"🔗 Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено друзей: <b>{(r['ref_count'] or 0) if r else 0}</b>\nБонусных публикаций: <b>{pub_bonus(uid)}</b>",
        reply_markup=kb)


@pub_router.message(F.text == PUB_BTN_INVITE)
async def pub_invite(m: Message, state: FSMContext):
    await state.clear()
    pub_user(m.from_user)
    await pub_send_invite(m, m.from_user.id)


@pub_router.callback_query(F.data == "ref:open")
async def pub_invite_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    pub_user(cb.from_user)
    await pub_send_invite(cb.message, cb.from_user.id)
    await cb.answer()


# ── свой бот пользователя (публикации и файлы идут от его бота) ──

def pub_tok_enc(t: str) -> str:
    f = _crypto()
    return "enc:" + f.encrypt(t.encode()).decode() if f else t


def pub_tok_dec(v: str) -> str:
    if v.startswith("enc:"):
        f = _crypto()
        if not f:
            raise ValueError("нет DB_ENCRYPT_KEY для расшифровки токена")
        return f.decrypt(v[4:].encode()).decode()
    return v


def pub_custom_row(uid: int):
    with db() as c:
        return c.execute("SELECT * FROM pub_bots WHERE owner_id=? AND status='active'", (uid,)).fetchone()


def pub_user_custom(uid: int):
    """(id личного бота, Bot) — если у пользователя он подключён, запущен и тариф позволяет; иначе (0, None)."""
    row = pub_custom_row(uid)
    if row and pub_plan_key(uid) in PUB_CUSTOM_BOT_PLANS and row["id"] in _custom:
        return row["id"], _custom[row["id"]]["bot"]
    return 0, None


def pub_bot_by_id(main: Bot, bot_id: int):
    """Бот, которым опубликован пост/канал: 0 — главный, иначе личный бот (или None, если не запущен)."""
    if not bot_id:
        return main
    info = _custom.get(bot_id)
    return info["bot"] if info else None


def pub_ctx_botid(d: dict) -> int:
    if d.get("edit_post"):
        with db() as c:
            r = c.execute("SELECT bot_id FROM pub_posts WHERE id=?", (d["edit_post"],)).fetchone()
        return (r["bot_id"] or 0) if r else 0
    return (d.get("draft") or {}).get("bot_id") or 0


async def pub_deep_deliver(m: Message, bot: Bot, arg: str, owner_id: int | None = None) -> bool:
    """/start b<id>: выдать содержимое кнопки (после проверки подписки). True — запрос обработан."""
    if not (arg.startswith("b") and arg[1:].isdigit()):
        return False
    with db() as c:
        row = c.execute("SELECT b.*, p.owner_id AS powner FROM pub_buttons b JOIN pub_posts p ON p.id=b.post_id "
                        "WHERE b.id=?", (int(arg[1:]),)).fetchone()
    if not row or row["action"] not in ("file", "text"):
        return False
    if owner_id is not None and row["powner"] != owner_id:
        return False
    ok, why = await pub_check_sub(bot, m.from_user.id, dict(row))
    if not ok:
        await m.answer(why)
        return True
    return await pub_deliver(bot, m.from_user.id, dict(row))


async def pub_port_file(main: Bot, pbot: Bot, owner: int, kind: str, fid: str, fname) -> str:
    """Переносит файл в личный бот (file_id у каждого бота свой). Возвращает file_id для личного бота."""
    try:
        data = (await main.download(fid)).getvalue()
        ext = {"photo": "jpg", "video": "mp4", "audio": "mp3", "voice": "ogg", "animation": "mp4"}.get(kind, "bin")
        msg = await getattr(pbot, "send_" + kind)(owner, **{kind: BufferedInputFile(data, filename=fname or f"file.{ext}")})
    except TelegramForbiddenError:
        raise PubError(f"Откройте своего бота @{(await pbot.me()).username}, нажмите Start и повторите.")
    except TelegramBadRequest as e:
        raise PubError(f"Не удалось перенести файл в ваш бот (лимит 20 МБ): {e}")
    _, new_fid = pub_media(msg)
    await pub_safe_delete(pbot, owner, msg.message_id)
    return new_fid


async def pub_prepare_custom(main: Bot, pbot: Bot, owner: int, draft: dict):
    """Готовит пост для публикации через личный бот: медиа загружается заново, файлы кнопок переносятся."""
    d2 = dict(draft)
    if draft["kind"] != "text":
        try:
            data = (await main.download(draft["file_id"])).getvalue()
        except TelegramBadRequest as e:
            raise PubError(f"Файл слишком большой для личного бота (лимит 20 МБ): {e}")
        ext = {"photo": "jpg", "video": "mp4", "audio": "mp3", "voice": "ogg", "animation": "mp4"}.get(draft["kind"], "bin")
        d2["file_id"] = BufferedInputFile(data, filename=draft.get("fname") or f"file.{ext}")
    btns = []
    for b in draft["buttons"]:
        b2 = dict(b)
        if b["action"] == "file":
            b2["payload"] = await pub_port_file(main, pbot, owner, b["file_kind"], b["payload"], b.get("fname"))
        btns.append(b2)
    return d2, btns


def make_click_router(owner_id: int) -> Router:
    """Обработчики личного бота: кнопки под постами и выдача файлов по /start."""
    r = Router()

    @r.callback_query(F.data.regexp(r"^pb:\d+$"))
    async def _click(cb: CallbackQuery, bot: Bot):
        await pub_handle_click(cb, bot, (await bot.me()).username, owner_id)

    @r.callback_query(F.data == "pv")
    async def _pv(cb: CallbackQuery):
        await cb.answer()

    @r.message(CommandStart())
    async def _start(m: Message, command: CommandObject, bot: Bot):
        if await pub_deep_deliver(m, bot, (command.args or "").strip(), owner_id):
            return
        await m.answer("👋 Здесь вы получаете файлы и материалы из постов канала.\n"
                       "Нажмите кнопку «Получить» под постом в канале.")

    return r


async def _pub_custom_poll(bid: int, cbot: Bot, dp: Dispatcher) -> None:
    try:
        await dp.start_polling(cbot, handle_signals=False, allowed_updates=["message", "callback_query"])
    except TelegramUnauthorizedError:
        log.warning("личный бот #%s: токен больше не действует", bid)
        with db() as c:
            c.execute("UPDATE pub_bots SET status='bad' WHERE id=?", (bid,))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("личный бот #%s остановился", bid)
    finally:
        try:
            await cbot.session.close()
        except Exception:
            pass
        if (_custom.get(bid) or {}).get("bot") is cbot:
            _custom.pop(bid, None)


async def pub_custom_start(bid: int) -> bool:
    if bid in _custom:
        return True
    with db() as c:
        row = c.execute("SELECT * FROM pub_bots WHERE id=?", (bid,)).fetchone()
    if not row:
        return False
    try:
        cbot = Bot(pub_tok_dec(row["token"]), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        me = await cbot.get_me()
        await cbot.delete_webhook(drop_pending_updates=False)
    except TelegramUnauthorizedError:
        with db() as c:
            c.execute("UPDATE pub_bots SET status='bad' WHERE id=?", (bid,))
        log.warning("личный бот #%s: неверный токен", bid)
        return False
    except Exception:
        log.exception("личный бот #%s не запустился", bid)
        return False
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(make_click_router(row["owner_id"]))
    task = asyncio.create_task(_pub_custom_poll(bid, cbot, dp))
    _custom[bid] = {"bot": cbot, "task": task, "owner": row["owner_id"], "username": me.username}
    return True


async def pub_custom_stop(bid: int) -> None:
    info = _custom.pop(bid, None)
    if info:
        info["task"].cancel()
        await asyncio.sleep(0)


@pub_router.callback_query(F.data == "cbot:add")
async def pub_cbot_add(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if pub_plan_key(uid) not in PUB_CUSTOM_BOT_PLANS:
        await cb.message.answer("🤖 <b>Свой бот</b> — функция тарифа <b>PRO</b>.\nФайлы и кнопки в ваших каналах "
                                "будут работать от имени вашего бота, без упоминания нашего.", reply_markup=pub_plans_kb())
        return await cb.answer()
    if pub_custom_row(uid):
        return await cb.answer("Свой бот уже подключён", show_alert=True)
    await state.set_state(PubBot.token)
    await cb.message.answer(
        "🤖 <b>Подключение своего бота</b>\n\n"
        "1️⃣ Откройте @BotFather → /newbot → создайте бота (название и аватарку лучше как у канала).\n"
        "2️⃣ Скопируйте <b>токен</b> и отправьте его сюда. Я сразу удалю сообщение и сохраню токен в зашифрованном виде.\n\n"
        "/cancel — отмена")
    await cb.answer()


@pub_router.message(PubBot.token, F.text)
async def pub_cbot_token(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    tok = m.text.strip()
    await pub_safe_delete(bot, m.chat.id, m.message_id)  # токен — секрет, не оставляем в чате
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", tok):
        return await m.answer("Это не похоже на токен. Он выглядит так: <code>123456789:AAH...</code>. Отправьте ещё раз.")
    if tok in (BOT_TOKEN, PUB_BOT_TOKEN):
        return await m.answer("Нужен токен вашего нового бота, а не этого.")
    try:
        tmp = Bot(tok, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        me = await tmp.get_me()
        await tmp.session.close()
    except (TelegramUnauthorizedError, TelegramBadRequest, ValueError):
        return await m.answer("Токен не подошёл. Проверьте его в @BotFather и отправьте ещё раз.")
    with db() as c:
        if c.execute("SELECT 1 FROM pub_bots WHERE tg_id=?", (me.id,)).fetchone():
            return await m.answer("Этот бот уже подключён.")
        bid = c.execute("INSERT INTO pub_bots(owner_id, token, username, tg_id, created) VALUES(?,?,?,?,?)",
                        (uid, pub_tok_enc(tok), me.username, me.id, int(time.time()))).lastrowid
    await state.clear()
    if not await pub_custom_start(bid):
        return await m.answer("Не удалось запустить бота. Попробуйте позже.")
    await m.answer(
        f"✅ Бот @{me.username} подключён!\n\n"
        f"1️⃣ Откройте @{me.username} и нажмите <b>Start</b> (нужно один раз).\n"
        "2️⃣ Добавьте его админом в канал (право «Публикация сообщений»).\n"
        "3️⃣ «Мой профиль» → «🔄 Перевести каналы на свой бот».\n\n"
        "Новые каналы теперь добавляются через ваш бот.", reply_markup=pub_main_kb())


@pub_router.callback_query(F.data == "cbot:move")
async def pub_cbot_move(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    bot_id, vb = pub_user_custom(uid)
    if not vb:
        await cb.message.answer("Свой бот не подключён или не запущен (нужен тариф PRO).")
        return await cb.answer()
    ok, bad = [], []
    for ch in pub_channels(uid):
        if ch["bot_id"] == bot_id:
            continue
        try:
            me = await vb.get_chat_member(ch["chat_id"], vb.id)
            good = me.status == ChatMemberStatus.ADMINISTRATOR and getattr(me, "can_post_messages", False)
        except (TelegramBadRequest, TelegramForbiddenError):
            good = False
        if good:
            with db() as c:
                c.execute("UPDATE pub_channels SET bot_id=? WHERE id=?", (bot_id, ch["id"]))
            ok.append(ch["title"] or str(ch["chat_id"]))
        else:
            bad.append(ch["title"] or str(ch["chat_id"]))
    text = "🔄 <b>Перенос каналов</b>\n"
    if ok:
        text += "\n✅ Переведены:\n" + "\n".join(f"• {html.escape(t)}" for t in ok)
    if bad:
        text += f"\n\n⚠️ Ваш бот не админ в:\n" + "\n".join(f"• {html.escape(t)}" for t in bad) + \
                "\nДобавьте его админом и нажмите ещё раз."
    if not ok and not bad:
        text += "\nВсе каналы уже работают через ваш бот."
    await cb.message.answer(text)
    await cb.answer()


@pub_router.callback_query(F.data == "cbot:off")
async def pub_cbot_off(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, отключить", callback_data="cbot:off2"),
        InlineKeyboardButton(text="Отмена", callback_data="pn:cancel")]])
    await cb.message.answer("⚠️ Если отключить бот, <b>кнопки в ранее опубликованных постах перестанут работать</b>, "
                            "а каналы вернутся на наш бот (его нужно будет снова добавить админом). Отключить?",
                            reply_markup=kb)
    await cb.answer()


@pub_router.callback_query(F.data == "cbot:off2")
async def pub_cbot_off2(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    row = pub_custom_row(uid)
    if row:
        await pub_custom_stop(row["id"])
        with db() as c:
            c.execute("DELETE FROM pub_bots WHERE id=?", (row["id"],))
            c.execute("UPDATE pub_channels SET bot_id=0 WHERE owner_id=?", (uid,))
    await pub_safe_delete(bot, cb.message.chat.id, cb.message.message_id)
    await cb.message.answer("Свой бот отключён. Токен удалён из базы.")
    await cb.answer()


# ── каналы ──

@pub_router.callback_query(F.data == "pc:add")
async def pub_chan_add(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    plan = PUB_PLANS[pub_plan_key(uid)]
    if len(pub_channels(uid)) >= plan["channels"]:
        await cb.message.answer(f"⛔ На тарифе {plan['name']} можно добавить до {plan['channels']} кан.\n"
                                "Подключите тариф выше 👇", reply_markup=pub_plans_kb())
        return await cb.answer()
    await state.set_state(PubChan.wait)
    _, vb = pub_user_custom(uid)
    who = f"вашего бота @{(await vb.me()).username}" if vb else "этого бота"
    await cb.message.answer(
        "📢 <b>Добавление канала</b>\n\n"
        f"1️⃣ Добавьте {who} в канал как <b>администратора</b> (право «Публикация сообщений»).\n"
        "2️⃣ Перешлите сюда любой пост из канала <b>или</b> отправьте @username канала.\n\n/cancel — отмена")
    await cb.answer()


@pub_router.message(PubChan.wait)
async def pub_chan_got(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    bot_id, vb = pub_user_custom(uid)
    vbot = vb or bot
    origin = getattr(m, "forward_origin", None)
    ref = None
    if origin is not None and getattr(origin, "chat", None) is not None:
        ref = origin.chat.id
    elif getattr(m, "forward_from_chat", None):
        ref = m.forward_from_chat.id
    elif m.text and not m.text.startswith("/"):
        t = m.text.strip().replace("https://t.me/", "@").replace("t.me/", "@")
        ref = int(t) if t.lstrip("-").isdigit() else (t if t.startswith("@") else "@" + t)
    if ref is None:
        return await m.answer("Перешлите пост из канала или отправьте @username канала.")
    try:
        chat = await vbot.get_chat(ref)
        if chat.type != "channel":
            return await m.answer("Это не канал. Нужен именно канал.")
        me = await vbot.get_chat_member(chat.id, vbot.id)
        if me.status != ChatMemberStatus.ADMINISTRATOR or not getattr(me, "can_post_messages", False):
            return await m.answer("Бот не администратор канала или нет права публикации. Исправьте и отправьте ещё раз.")
        who = await vbot.get_chat_member(chat.id, uid)
        if who.status not in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
            return await m.answer("Вы не администратор этого канала.")
    except (TelegramBadRequest, TelegramForbiddenError):
        return await m.answer("Не нашёл канал или у бота нет доступа. Добавьте бота админом и повторите.")
    plan = PUB_PLANS[pub_plan_key(uid)]
    with db() as c:
        have = c.execute("SELECT 1 FROM pub_channels WHERE owner_id=? AND chat_id=?", (uid, chat.id)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM pub_channels WHERE owner_id=?", (uid,)).fetchone()[0]
        if have:
            await state.clear()
            return await m.answer("Этот канал уже добавлен ✅")
        if n >= plan["channels"]:
            await state.clear()
            return await m.answer(f"⛔ Лимит каналов на тарифе {plan['name']}: {plan['channels']}.", reply_markup=pub_plans_kb())
        c.execute("INSERT INTO pub_channels(owner_id, chat_id, title, username, bot_id) VALUES(?,?,?,?,?)",
                  (uid, chat.id, chat.title, chat.username, bot_id))
    await state.clear()
    await m.answer(f"✅ Канал <b>{html.escape(chat.title or '')}</b> добавлен!\nТеперь нажмите «{PUB_BTN_NEW}».",
                   reply_markup=pub_main_kb())


@pub_router.callback_query(F.data.regexp(r"^pc:del:\d+$"))
async def pub_chan_del(cb: CallbackQuery):
    with db() as c:
        c.execute("DELETE FROM pub_channels WHERE id=? AND owner_id=?", (int(cb.data.split(":")[2]), cb.from_user.id))
    await cb.message.answer("🗑 Канал убран из бота. Откройте «Мой профиль» ещё раз, чтобы обновить список.")
    await cb.answer()


# ── новая публикация ──

@pub_router.message(F.text == PUB_BTN_NEW)
async def pub_new(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    pub_user(m.from_user)
    chans = pub_channels(uid)
    if not chans:
        return await m.answer("У вас пока нет каналов. Сначала добавьте канал:", reply_markup=pub_add_channel_kb())
    plan = PUB_PLANS[pub_plan_key(uid)]
    if pub_daily_blocked(uid):
        return await m.answer(f"⛔ Лимит публикаций на сегодня исчерпан ({plan['daily']} на тарифе {plan['name']}).\n"
                              f"Пригласите друга — получите +{PUB_REF_BONUS} публикации за каждого, "
                              "или подключите тариф выше 👇", reply_markup=pub_limit_kb())
    kb = InlineKeyboardBuilder()
    for ch in chans[:plan["channels"]]:
        kb.button(text=f"📢 {ch['title']}", callback_data=f"pn:ch:{ch['id']}")
    kb.button(text="❌ Отмена", callback_data="pn:cancel")
    kb.adjust(1)
    await m.answer("Выберите канал для публикации:", reply_markup=kb.as_markup())


@pub_router.callback_query(F.data.regexp(r"^pn:ch:\d+$"))
async def pub_pick_channel(cb: CallbackQuery, state: FSMContext):
    with db() as c:
        ch = c.execute("SELECT * FROM pub_channels WHERE id=? AND owner_id=?",
                       (int(cb.data.split(":")[2]), cb.from_user.id)).fetchone()
    if not ch:
        return await cb.answer("Канал не найден", show_alert=True)
    err = pub_limit_error(cb.from_user.id, ch["chat_id"])
    if err:
        return await cb.answer(err, show_alert=True)
    await state.set_state(PubNew.content)
    await state.update_data(draft={"chat_id": ch["chat_id"], "title": ch["title"], "username": ch["username"],
                                   "kind": "text", "file_id": "", "text": "", "buttons": [],
                                   "bot_id": ch["bot_id"] or 0}, prev=[])
    await cb.message.edit_text(
        f"📢 Канал: <b>{html.escape(ch['title'] or '')}</b>\n\n"
        "Отправьте <b>фото, видео, аудио, музыку, голосовое или файл</b> — подпись напишите прямо под ним.\n"
        "Можно и просто текст.")
    await cb.answer()


@pub_router.callback_query(F.data == "pn:cancel")
async def pub_new_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    for mid in d.get("prev", []):
        await pub_safe_delete(bot, cb.message.chat.id, mid)
    await state.clear()
    await pub_safe_delete(bot, cb.message.chat.id, cb.message.message_id)
    await cb.answer("Отменено")


@pub_router.message(PubNew.content)
async def pub_content(m: Message, state: FSMContext, bot: Bot):
    kind, fid = pub_media(m)
    if kind is None:
        if m.text and not m.text.startswith("/"):
            kind, fid = "text", ""
        else:
            return await m.answer("Отправьте фото, видео, аудио, голосовое, файл или текст 🙂")
    text = pub_html(m)
    if len(text) > (4000 if kind == "text" else 1000):
        return await m.answer("Слишком длинный текст. Для медиа подпись — до 1000 символов, для текста — до 4000.")
    d = await state.get_data()
    draft = d["draft"]
    draft.update(kind=kind, file_id=fid, text=text,
                 fname=getattr(m.document or m.audio or m.video or m.animation, "file_name", None))
    await state.update_data(draft=draft)
    if kind != "text" and not text:
        await state.set_state(PubNew.caption)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭ Без подписи", callback_data="pn:skipcap")]])
        return await m.answer("✍️ Напишите подпись под постом (или нажмите «Без подписи»):", reply_markup=kb)
    await pub_show_draft(bot, m.chat.id, state)


@pub_router.message(PubNew.caption, F.text)
async def pub_caption(m: Message, state: FSMContext, bot: Bot):
    if len(m.html_text) > 1000:
        return await m.answer("Подпись слишком длинная (до 1000 символов).")
    d = await state.get_data()
    d["draft"]["text"] = m.html_text
    await state.update_data(draft=d["draft"])
    await pub_show_draft(bot, m.chat.id, state)


@pub_router.callback_query(PubNew.caption, F.data == "pn:skipcap")
async def pub_skip_caption(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await pub_safe_delete(bot, cb.message.chat.id, cb.message.message_id)
    await pub_show_draft(bot, cb.message.chat.id, state)
    await cb.answer()


async def pub_show_draft(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Показывает предпросмотр поста и меню «добавить кнопку / сохранить»."""
    d = await state.get_data()
    draft = d["draft"]
    for mid in d.get("prev", []):
        await pub_safe_delete(bot, chat_id, mid)
    await state.set_state(PubNew.confirm)
    prev = []
    try:
        s = await pub_send(bot, chat_id, draft,
                           pub_build_markup([dict(b, post_text=draft["text"]) for b in draft["buttons"]]))
        prev.append(s.message_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await bot.send_message(chat_id, f"Не удалось показать предпросмотр: {html.escape(str(e))}")
    n = len(draft["buttons"])
    kb = InlineKeyboardBuilder()
    if n < PUB_MAX_BUTTONS:
        kb.button(text="➕ Добавить кнопку" if not n else "➕ Ещё кнопку", callback_data="pn:addbtn")
    kb.button(text="💾 Сохранить и опубликовать", callback_data="pn:save")
    kb.button(text="❌ Отмена", callback_data="pn:cancel")
    kb.adjust(1)
    s2 = await bot.send_message(
        chat_id, "Добавить кнопку в пост?" if not n else f"Кнопок: {n}. Добавить ещё или публиковать?",
        reply_markup=kb.as_markup())
    prev.append(s2.message_id)
    await state.update_data(prev=prev)


@pub_router.callback_query(F.data == "pv")
async def pub_preview_noop(cb: CallbackQuery):
    await cb.answer("Это предпросмотр — кнопка заработает в канале", show_alert=True)


@pub_router.callback_query(PubNew.confirm, F.data == "pn:save")
async def pub_save(cb: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    draft = d.get("draft")
    uid = cb.from_user.id
    if not draft:
        return await cb.answer("Сессия устарела, начните заново", show_alert=True)
    err = pub_limit_error(uid, draft["chat_id"])
    if err:
        return await cb.answer(err, show_alert=True)
    bot_id = draft.get("bot_id") or 0
    pbot = pub_bot_by_id(bot, bot_id)  # главный бот или личный бот пользователя
    if pbot is None:
        await cb.message.answer("❌ Ваш личный бот сейчас не запущен. Откройте «Мой профиль» → «Свой бот».")
        return await pub_ack(cb)
    send_draft, btns = draft, draft["buttons"]
    if bot_id:  # файлы нужно перенести в личный бот: у каждого бота свои file_id
        await cb.answer("⏳ Публикую через ваш бот...")
        try:
            send_draft, btns = await pub_prepare_custom(bot, pbot, uid, draft)
        except PubError as e:
            await cb.message.answer(f"❌ {html.escape(str(e))}")
            return
    used_bonus = pub_take_bonus(uid)
    with db() as c:
        pid = c.execute(
            "INSERT INTO pub_posts(owner_id, chat_id, title, message_id, kind, file_id, text, created, bot_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (uid, draft["chat_id"], draft["title"], 0, draft["kind"], draft["file_id"], draft["text"], int(time.time()), bot_id)).lastrowid
        for b in btns:
            pub_insert_button(c, pid, b)
    try:
        sent = await pub_send(pbot, draft["chat_id"], send_draft, pub_build_markup(pub_post_items(pid)))
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        with db() as c:
            c.execute("DELETE FROM pub_buttons WHERE post_id=?", (pid,))
            c.execute("DELETE FROM pub_posts WHERE id=?", (pid,))
            if used_bonus:
                c.execute("UPDATE pub_users SET bonus_posts=bonus_posts+1 WHERE user_id=?", (uid,))
        await cb.message.answer("❌ Не удалось опубликовать: " + html.escape(str(e)) +
                                "\nПроверьте, что бот — админ канала с правом публикации.")
        return await pub_ack(cb)
    with db() as c:
        c.execute("UPDATE pub_posts SET message_id=? WHERE id=?", (sent.message_id, pid))
    for mid in d.get("prev", []):
        await pub_safe_delete(bot, cb.message.chat.id, mid)
    await state.clear()
    link = pub_post_link(draft["chat_id"], draft.get("username"), sent.message_id)
    await cb.message.answer(f"✅ <b>Опубликовано</b> в «{html.escape(draft['title'] or '')}»!\n{link}", reply_markup=pub_main_kb())
    await pub_ack(cb)


# ── кнопка под постом: название → действие → значение ──

@pub_router.callback_query(F.data == "pn:addbtn")
async def pub_btn_begin(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PubNew.btn_label)
    await cb.message.edit_text("🔘 Введите <b>название кнопки</b> (до 40 символов):")
    await cb.answer()


@pub_router.message(PubNew.btn_label, F.text)
async def pub_btn_label(m: Message, state: FSMContext):
    label = m.text.strip()
    if not label or len(label) > 40:
        return await m.answer("Название — от 1 до 40 символов. Попробуйте ещё раз.")
    await state.update_data(btn_label=label)
    await state.set_state(PubNew.btn_action)
    kb = InlineKeyboardBuilder()
    for key, title in PUB_ACTIONS.items():
        kb.button(text=title, callback_data=f"pa:{key}")
    kb.adjust(1)
    await m.answer(f"Что будет делать кнопка «{html.escape(label)}»?", reply_markup=kb.as_markup())


PUB_VALUE_ASK = {
    "file": "📁 Отправьте <b>файл, фото, видео или музыку</b>. Пользователь получит его в личку при нажатии. "
            "Подпись к файлу тоже сохранится.",
    "link": "🔗 Отправьте <b>ссылку</b> (https://... или @username):",
    "text": "💬 Отправьте <b>текст</b>, который получит пользователь в личку:",
    "alert": "🔔 Отправьте <b>короткий текст</b> всплывающего окна (до 200 символов):",
}


@pub_router.callback_query(PubNew.btn_action, F.data.startswith("pa:"))
async def pub_btn_action(cb: CallbackQuery, state: FSMContext, bot: Bot):
    action = cb.data.split(":")[1]
    if action not in PUB_ACTIONS:
        return await cb.answer()
    if action == "dm":
        uname = cb.from_user.username
        if not uname:
            return await cb.answer("Чтобы вам могли написать, задайте имя пользователя (@username) в настройках Telegram и выберите действие снова.",
                                   show_alert=True)
        await state.update_data(btn_action=action)
        await cb.message.edit_text(
            f"📩 <b>Переслать текст в личку</b>\n\nС помощью этой кнопки пользователи вашего канала отправят текст этого поста "
            f"вам в личку (@{html.escape(uname)}).\n\nПри нажатии откроется чат с вами, а в поле ввода уже будет готово "
            "сообщение «Здравствуйте! Хочу узнать об этом подробнее» и текст поста — останется нажать «Отправить».")
        await pub_ask_color(bot, cb.message.chat.id, cb.from_user.id, state, {"payload": uname})
        return await cb.answer()
    await state.update_data(btn_action=action)
    if action == "like":
        await cb.message.delete()
        await pub_ask_color(bot, cb.message.chat.id, cb.from_user.id, state, {"payload": ""})
        return await cb.answer()
    await state.set_state(PubNew.btn_value)
    await cb.message.edit_text(PUB_VALUE_ASK[action])
    await cb.answer()


@pub_router.message(PubNew.btn_value)
async def pub_btn_value(m: Message, state: FSMContext, bot: Bot):
    action = (await state.get_data())["btn_action"]
    val: dict = {}
    if action == "file":
        kind, fid = pub_media(m)
        if not kind:
            return await m.answer("Отправьте файл, фото, видео или аудио.")
        val = {"payload": fid, "file_kind": kind, "extra": pub_html(m),
               "fname": getattr(m.document or m.audio or m.video or m.animation, "file_name", None)}
    elif action == "link":
        t = (m.text or "").strip()
        if t.startswith("@"):
            t = "https://t.me/" + t[1:]
        elif t.startswith("t.me/"):
            t = "https://" + t
        if not t.startswith(("http://", "https://", "tg://")) or " " in t:
            return await m.answer("Нужна ссылка вида https://... или @username.")
        val = {"payload": t}
    elif action == "text":
        if not m.text or len(m.html_text) > 3500:
            return await m.answer("Отправьте текст (до 3500 символов).")
        val = {"payload": m.html_text}
    elif action == "alert":
        if not m.text or len(m.text) > 200:
            return await m.answer("Отправьте текст до 200 символов.")
        val = {"payload": m.text}
    if action in ("file", "text"):
        await pub_ask_cond(bot, m.chat.id, state, val)
    else:
        await pub_ask_color(bot, m.chat.id, m.from_user.id, state, val)


def pub_ctx(d: dict):
    """(chat_id, название, username) канала, куда идёт пост (или где он уже опубликован)."""
    if d.get("edit_post"):
        with db() as c:
            r = c.execute("SELECT chat_id, title FROM pub_posts WHERE id=?", (d["edit_post"],)).fetchone()
            ch = c.execute("SELECT username FROM pub_channels WHERE chat_id=? LIMIT 1", (r["chat_id"],)).fetchone() if r else None
        return (r["chat_id"], r["title"], ch["username"] if ch else None) if r else (None, None, None)
    dr = d["draft"]
    return dr["chat_id"], dr["title"], dr.get("username")


async def pub_ask_cond(bot: Bot, chat_id: int, state: FSMContext, val: dict) -> None:
    await state.update_data(btn_val=val)
    await state.set_state(PubNew.btn_cond)
    _, title, _ = pub_ctx(await state.get_data())
    kb = InlineKeyboardBuilder()
    kb.button(text="🆓 Без условий", callback_data="pcnd:none")
    kb.button(text=f"📢 Подписка на «{(title or '')[:25]}»", callback_data="pcnd:this")
    kb.button(text="➕ Подписка на другой канал", callback_data="pcnd:other")
    kb.adjust(1)
    await bot.send_message(
        chat_id, "🔒 <b>Условие получения</b>\n\nМожно потребовать подписку на канал. Проверка идёт прямо в канале "
                 "(всплывающим окном): подписанный получит содержимое, остальным бот подскажет, на что подписаться.",
        reply_markup=kb.as_markup())


@pub_router.callback_query(PubNew.btn_cond, F.data.startswith("pcnd:"))
async def pub_cond_pick(cb: CallbackQuery, state: FSMContext, bot: Bot):
    mode = cb.data.split(":")[1]
    d = await state.get_data()
    val = d.get("btn_val") or {}
    await pub_safe_delete(bot, cb.message.chat.id, cb.message.message_id)
    if mode == "other":
        await state.set_state(PubNew.btn_need)
        await cb.message.answer("📢 Перешлите пост из нужного канала или отправьте его @username.\n"
                                "Бот должен быть админом в этом канале, иначе не сможет проверять подписку.")
        return await cb.answer()
    if mode == "this":
        chat_id, title, uname = pub_ctx(d)
        val.update(need_chat=chat_id, need_title=title, need_link=f"https://t.me/{uname}" if uname else None)
    await pub_ask_color(bot, cb.message.chat.id, cb.from_user.id, state, val)
    await cb.answer()


@pub_router.message(PubNew.btn_need)
async def pub_need_got(m: Message, state: FSMContext, bot: Bot):
    d0 = await state.get_data()
    vbot = pub_bot_by_id(bot, pub_ctx_botid(d0)) or bot
    origin = getattr(m, "forward_origin", None)
    ref = None
    if origin is not None and getattr(origin, "chat", None) is not None:
        ref = origin.chat.id
    elif getattr(m, "forward_from_chat", None):
        ref = m.forward_from_chat.id
    elif m.text and not m.text.startswith("/"):
        t = m.text.strip().replace("https://t.me/", "@").replace("t.me/", "@")
        ref = int(t) if t.lstrip("-").isdigit() else (t if t.startswith("@") else "@" + t)
    if ref is None:
        return await m.answer("Перешлите пост из канала или отправьте @username.")
    try:
        chat = await vbot.get_chat(ref)
        if chat.type not in ("channel", "supergroup"):
            return await m.answer("Нужен канал (или группа).")
        me = await vbot.get_chat_member(chat.id, vbot.id)
        if me.status != ChatMemberStatus.ADMINISTRATOR:
            return await m.answer("Бот не админ в этом канале — сделайте его админом и отправьте ещё раз.")
        who = await vbot.get_chat_member(chat.id, m.from_user.id)
        if who.status not in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
            return await m.answer("Вы не администратор этого канала.")
    except (TelegramBadRequest, TelegramForbiddenError):
        return await m.answer("Не нашёл канал или у бота нет доступа. Добавьте бота админом и повторите.")
    val = (await state.get_data()).get("btn_val") or {}
    val.update(need_chat=chat.id, need_title=chat.title, need_link=f"https://t.me/{chat.username}" if chat.username else None)
    await pub_ask_color(bot, m.chat.id, m.from_user.id, state, val)


async def pub_ask_layout(bot: Bot, chat_id: int, state: FSMContext, val: dict) -> None:
    """Спрашивает, ставить ли кнопку рядом с предыдущей (2 в один ряд)."""
    d = await state.get_data()
    items = pub_post_items(d["edit_post"]) if d.get("edit_post") else d["draft"]["buttons"]
    if not items or len(pub_group_rows(items)[-1]) >= 2:
        val["same_row"] = 0
        return await pub_btn_commit(bot, chat_id, state, val)
    await state.update_data(btn_val=val)
    await state.set_state(PubNew.btn_layout)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬇️ С новой строки", callback_data="play:new")
    kb.button(text="↔️ Рядом с предыдущей (2 в ряд)", callback_data="play:same")
    kb.adjust(1)
    await bot.send_message(chat_id, "Где разместить кнопку?", reply_markup=kb.as_markup())


@pub_router.callback_query(PubNew.btn_layout, F.data.startswith("play:"))
async def pub_layout_pick(cb: CallbackQuery, state: FSMContext, bot: Bot):
    val = (await state.get_data()).get("btn_val") or {}
    val["same_row"] = 1 if cb.data.endswith("same") else 0
    await pub_safe_delete(bot, cb.message.chat.id, cb.message.message_id)
    await pub_btn_commit(bot, cb.message.chat.id, state, val)
    await cb.answer()


async def pub_ask_color(bot: Bot, chat_id: int, uid: int, state: FSMContext, val: dict) -> None:
    """Шаг «цвет кнопки»: выбор — на STANDART и PRO; на FREE кнопка обычная."""
    await state.update_data(btn_val=val)
    await state.set_state(PubNew.btn_color)
    kb = InlineKeyboardBuilder()
    if pub_plan_key(uid) == "free":
        kb.button(text="🎨 Цвет кнопки 🔒 (STANDART/PRO)", callback_data="pcol:lock")
        kb.button(text="➡️ Продолжить без цвета", callback_data="pcol:none")
        text = "🎨 Цвет кнопки доступен на тарифах <b>STANDART</b> и <b>PRO</b>."
    else:
        for key, title in PUB_COLORS.items():
            kb.button(text=title, callback_data=f"pcol:{key}")
        text = "🎨 Выберите <b>цвет кнопки</b>:"
    kb.adjust(1)
    await bot.send_message(chat_id, text, reply_markup=kb.as_markup())


@pub_router.callback_query(PubNew.btn_color, F.data.startswith("pcol:"))
async def pub_btn_color(cb: CallbackQuery, state: FSMContext, bot: Bot):
    color = cb.data.split(":")[1]
    if color == "lock" or (color != "none" and pub_plan_key(cb.from_user.id) == "free"):
        await cb.answer("🎨 Цвета кнопок — на тарифах STANDART и PRO", show_alert=True)
        return await cb.message.answer(pub_plans_text(cb.from_user.id), reply_markup=pub_plans_kb())
    if color != "none" and color not in PUB_COLORS:
        return await cb.answer()
    val = (await state.get_data()).get("btn_val") or {"payload": ""}
    val["style"] = None if color == "none" else color
    await pub_safe_delete(bot, cb.message.chat.id, cb.message.message_id)
    await pub_ask_layout(bot, cb.message.chat.id, state, val)
    await cb.answer()


async def pub_btn_commit(bot: Bot, chat_id: int, state: FSMContext, val: dict) -> None:
    d = await state.get_data()
    btn = {"label": d["btn_label"], "action": d["btn_action"], "payload": val.get("payload", ""),
           "file_kind": val.get("file_kind"), "extra": val.get("extra"), "style": val.get("style"),
           "same_row": val.get("same_row", 0), "need_chat": val.get("need_chat"),
           "need_title": val.get("need_title"), "need_link": val.get("need_link"),
           "fname": val.get("fname")}
    edit_post = d.get("edit_post")
    if edit_post:  # кнопка добавляется к уже опубликованному посту
        with db() as c:
            post = c.execute("SELECT * FROM pub_posts WHERE id=?", (edit_post,)).fetchone()
        pbot = pub_bot_by_id(bot, post["bot_id"] or 0)
        await state.clear()
        if pbot is None:
            return await bot.send_message(chat_id, "❌ Личный бот этого поста сейчас не запущен.")
        if post["bot_id"] and btn["action"] == "file":
            try:
                btn["payload"] = await pub_port_file(bot, pbot, post["owner_id"], btn["file_kind"], btn["payload"], val.get("fname"))
            except PubError as e:
                return await bot.send_message(chat_id, f"❌ {html.escape(str(e))}")
        with db() as c:
            pub_insert_button(c, edit_post, btn)
        try:
            await pbot.edit_message_reply_markup(chat_id=post["chat_id"], message_id=post["message_id"],
                                                reply_markup=pub_build_markup(pub_post_items(edit_post)))
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            await bot.send_message(chat_id, "Кнопка сохранена, но обновить пост не удалось: " + html.escape(str(e)))
        await bot.send_message(chat_id, "✅ Кнопка добавлена в пост.", reply_markup=pub_post_menu_kb(edit_post))
        return
    d["draft"]["buttons"].append(btn)
    await state.update_data(draft=d["draft"])
    await pub_show_draft(bot, chat_id, state)


# ── нажатия на кнопки в канале ──

@pub_router.callback_query(F.data.regexp(r"^pb:\d+$"))
async def pub_button_click(cb: CallbackQuery, bot: Bot):
    await pub_handle_click(cb, bot, _pub_username)


async def pub_handle_click(cb: CallbackQuery, bot: Bot, username: str, owner_id: int | None = None):
    """Нажатие кнопки под постом. Работает и в главном боте, и в личных ботах пользователей."""
    bid = int(cb.data.split(":")[1])
    with db() as c:
        row = c.execute("SELECT * FROM pub_buttons WHERE id=?", (bid,)).fetchone()
        if row:
            c.execute("UPDATE pub_buttons SET clicks=clicks+1 WHERE id=?", (bid,))
    if not row:
        return await cb.answer("Эта кнопка больше не активна", show_alert=True)
    b = dict(row)
    if owner_id is not None:  # личный бот обслуживает только посты своего владельца
        with db() as c:
            post = c.execute("SELECT owner_id FROM pub_posts WHERE id=?", (b["post_id"],)).fetchone()
        if not post or post["owner_id"] != owner_id:
            return await cb.answer("Кнопка недоступна", show_alert=True)
    if b["action"] == "alert":
        return await cb.answer(b["payload"][:200], show_alert=True)
    if b["action"] == "like":
        uid = cb.from_user.id
        with db() as c:
            if c.execute("SELECT 1 FROM pub_likes WHERE button_id=? AND user_id=?", (bid, uid)).fetchone():
                c.execute("DELETE FROM pub_likes WHERE button_id=? AND user_id=?", (bid, uid))
                note = "Лайк убран"
            else:
                c.execute("INSERT INTO pub_likes(button_id, user_id) VALUES(?,?)", (bid, uid))
                note = "Спасибо! 👍"
        try:
            await cb.message.edit_reply_markup(reply_markup=pub_build_markup(pub_post_items(b["post_id"])))
        except TelegramBadRequest:
            pass
        return await cb.answer(note)
    ok, why = await pub_check_sub(bot, cb.from_user.id, b)  # проверка подписки — прямо в канале, всплывающим окном
    if not ok:
        return await cb.answer(why, show_alert=True)
    if await pub_deliver(bot, cb.from_user.id, b):
        return await cb.answer("✅ Отправил вам в личные сообщения")
    # бот ещё не запущен у пользователя — открываем бота, он сразу выдаст содержимое кнопки
    await cb.answer(url=f"https://t.me/{username}?start=b{bid}")


# ── редактирование публикаций ──

def pub_post_menu_kb(pid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить подпись/текст", callback_data=f"pe:cap:{pid}")
    kb.button(text="➕ Добавить кнопку", callback_data=f"pe:btn:{pid}")
    kb.button(text="🧹 Убрать все кнопки", callback_data=f"pe:clr:{pid}")
    kb.button(text="📊 Статистика", callback_data=f"pe:st:{pid}")
    kb.button(text="🗑 Удалить пост", callback_data=f"pe:del:{pid}")
    kb.button(text="⬅️ К списку", callback_data="pe:list")
    kb.adjust(1)
    return kb.as_markup()


def pub_own_post(uid: int, pid: int):
    with db() as c:
        return c.execute("SELECT * FROM pub_posts WHERE id=? AND owner_id=?", (pid, uid)).fetchone()


async def pub_send_list(target: Message, uid: int) -> None:
    with db() as c:
        rows = c.execute("SELECT * FROM pub_posts WHERE owner_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    if not rows:
        return await target.answer("У вас пока нет публикаций.")
    kb = InlineKeyboardBuilder()
    for r in rows:
        snippet = re.sub(r"<[^>]+>", "", r["text"] or "") or r["kind"]
        kb.button(text=f"#{r['id']} • {pub_date(r['created'])} • {snippet[:22]}", callback_data=f"pe:{r['id']}")
    kb.adjust(1)
    await target.answer("✏️ <b>Ваши последние публикации</b> — выберите:", reply_markup=kb.as_markup())


@pub_router.message(F.text == PUB_BTN_EDIT)
async def pub_edit_list(m: Message, state: FSMContext):
    await state.clear()
    pub_user(m.from_user)
    await pub_send_list(m, m.from_user.id)


@pub_router.callback_query(F.data == "pe:list")
async def pub_edit_list_cb(cb: CallbackQuery):
    await pub_send_list(cb.message, cb.from_user.id)
    await cb.answer()


@pub_router.callback_query(F.data.regexp(r"^pe:\d+$"))
async def pub_edit_open(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    post = pub_own_post(cb.from_user.id, pid)
    if not post:
        return await cb.answer("Публикация не найдена", show_alert=True)
    await cb.message.edit_text(
        f"📄 Публикация <b>#{pid}</b> • {html.escape(post['title'] or '')} • {pub_date(post['created'])}",
        reply_markup=pub_post_menu_kb(pid))
    await cb.answer()


@pub_router.callback_query(F.data.regexp(r"^pe:(cap|btn|clr|st|del):\d+$"))
async def pub_edit_action(cb: CallbackQuery, state: FSMContext, bot: Bot):
    _, act, spid = cb.data.split(":")
    pid = int(spid)
    post = pub_own_post(cb.from_user.id, pid)
    if not post:
        return await cb.answer("Публикация не найдена", show_alert=True)
    pbot = pub_bot_by_id(bot, post["bot_id"] or 0) if act in ("clr", "del") else bot
    if pbot is None:
        return await cb.answer("Личный бот этого поста не запущен. Проверьте «Мой профиль» → «Свой бот».", show_alert=True)
    if act == "cap":
        await state.set_state(PubEdit.caption)
        await state.update_data(edit_post=pid)
        await cb.message.answer("✍️ Отправьте новый текст/подпись для поста:")
    elif act == "btn":
        if len(pub_post_items(pid)) >= PUB_MAX_BUTTONS:
            return await cb.answer(f"Максимум {PUB_MAX_BUTTONS} кнопок", show_alert=True)
        await state.set_state(PubNew.btn_label)
        await state.update_data(edit_post=pid)
        await cb.message.answer("🔘 Введите <b>название кнопки</b> (до 40 символов):")
    elif act == "clr":
        with db() as c:
            c.execute("DELETE FROM pub_buttons WHERE post_id=?", (pid,))
        try:
            await pbot.edit_message_reply_markup(chat_id=post["chat_id"], message_id=post["message_id"], reply_markup=None)
        except TelegramBadRequest:
            pass
        await cb.message.answer("🧹 Кнопки убраны.")
    elif act == "st":
        items = pub_post_items(pid)
        lines = [f"📊 <b>Публикация #{pid}</b>"]
        if not items:
            lines.append("Кнопок нет.")
        for b in items:
            extra = f", лайков: {b['likes']}" if b["action"] == "like" else ""
            lines.append(f"• {html.escape(b['label'])} — нажатий: {b['clicks']}{extra}")
        await cb.message.answer("\n".join(lines))
    elif act == "del":
        try:
            await pbot.delete_message(post["chat_id"], post["message_id"])
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        with db() as c:
            c.execute("DELETE FROM pub_buttons WHERE post_id=?", (pid,))
            c.execute("DELETE FROM pub_posts WHERE id=?", (pid,))
        await cb.message.edit_text("🗑 Публикация удалена.")
    await cb.answer()


@pub_router.message(PubEdit.caption, F.text)
async def pub_edit_caption(m: Message, state: FSMContext, bot: Bot):
    pid = (await state.get_data()).get("edit_post")
    post = pub_own_post(m.from_user.id, pid) if pid else None
    if not post:
        await state.clear()
        return await m.answer("Публикация не найдена.")
    text = m.html_text
    if len(text) > (4000 if post["kind"] == "text" else 1000):
        return await m.answer("Слишком длинный текст.")
    markup = pub_build_markup([dict(b, post_text=text) for b in pub_post_items(pid)])
    pbot = pub_bot_by_id(bot, post["bot_id"] or 0)
    if pbot is None:
        await state.clear()
        return await m.answer("❌ Личный бот этого поста сейчас не запущен.")
    try:
        if post["kind"] == "text":
            await pbot.edit_message_text(text, chat_id=post["chat_id"], message_id=post["message_id"], reply_markup=markup)
        else:
            await pbot.edit_message_caption(chat_id=post["chat_id"], message_id=post["message_id"], caption=text, reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        return await m.answer("Не удалось изменить: " + html.escape(str(e)))
    with db() as c:
        c.execute("UPDATE pub_posts SET text=? WHERE id=?", (text, pid))
    await state.clear()
    await m.answer("✅ Текст обновлён.", reply_markup=pub_post_menu_kb(pid))


# ── тарифы и оплата ──

@pub_router.callback_query(F.data.regexp(r"^pp:(standard|pro)$"))
async def pub_pay_start(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    p = PUB_PLANS[key]
    numbers = "\n".join(f"{name} — <code>{num}</code>" for name, num in PUB_PAY_LINES)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="pp:cancel")]])
    await cb.message.edit_text(
        f"💳 <b>Оплата тарифа {p['name']} — {p['price']} сомони</b>\n\n"
        f"Переведите <b>{p['price']} сомони</b> на номер:\n{numbers}\n\n"
        "После оплаты отправьте сюда <b>чек платежа</b> (скриншот или фото).", reply_markup=kb)
    await state.set_state(PubPay.receipt)
    await state.update_data(plan=key, instr=cb.message.message_id)
    await cb.answer()


@pub_router.callback_query(F.data == "pp:cancel")
async def pub_pay_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(pub_plans_text(cb.from_user.id), reply_markup=pub_plans_kb())
    await cb.answer()


@pub_router.message(PubPay.receipt, F.photo | F.document)
async def pub_receipt(m: Message, state: FSMContext, bot: Bot):
    if m.document and not (m.document.mime_type or "").startswith("image/"):
        return await m.answer("Отправьте чек как фото или скриншот 🧾")
    d = await state.get_data()
    await state.clear()
    uid = m.from_user.id
    p = PUB_PLANS[d["plan"]]
    fid, kind = (m.photo[-1].file_id, "photo") if m.photo else (m.document.file_id, "doc")
    with db() as c:
        pid = c.execute(
            "INSERT INTO pub_payments(user_id, username, plan, amount, receipt, receipt_kind, created) VALUES(?,?,?,?,?,?,?)",
            (uid, m.from_user.username, d["plan"], p["price"], fid, kind, int(time.time()))).lastrowid
    await pub_safe_delete(bot, uid, d["instr"])  # сообщение с реквизитами удаляется
    wait = await m.answer("⏳ <b>Ваш платёж в обработке.</b>\nПожалуйста, подождите.")
    with db() as c:
        c.execute("UPDATE pub_payments SET wait_mid=? WHERE id=?", (wait.message_id, pid))
    who = html.escape(m.from_user.full_name) + (f" (@{m.from_user.username})" if m.from_user.username else "")
    caption = (f"💳 <b>Платёж #{pid}</b>\nОт: {who}\nID: <code>{uid}</code>\n"
               f"Тариф: <b>{p['name']}</b> — {p['price']} сомони")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"ppok:{pid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ppno:{pid}")]])
    _pub_pay_msgs[pid] = []
    for aid in PUB_ADMIN_IDS:
        try:
            send = bot.send_photo if kind == "photo" else bot.send_document
            sent = await send(aid, fid, caption=caption, reply_markup=kb)
            _pub_pay_msgs[pid].append((aid, sent.message_id, caption))
        except Exception as e:
            log.warning("не смог отправить чек админу %s: %s", aid, e)


@pub_router.message(PubPay.receipt)
async def pub_receipt_wrong(m: Message):
    await m.answer("Отправьте чек как фото или скриншот 🧾")


async def pub_close_admin_msgs(bot: Bot, pid: int, status: str, fallback: Message | None = None) -> None:
    items = _pub_pay_msgs.pop(pid, [])
    if not items and fallback:
        items = [(fallback.chat.id, fallback.message_id, html.escape(fallback.caption or ""))]
    for aid, mid, base in items:
        try:
            await bot.edit_message_caption(chat_id=aid, message_id=mid, caption=f"{base}\n\n{status}", reply_markup=None)
        except Exception:
            pass


@pub_router.callback_query(F.data.startswith("ppok:"), PUB_ADMIN)
async def pub_pay_ok(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[1])
    with db() as c:
        changed = c.execute("UPDATE pub_payments SET status='ok' WHERE id=? AND status='pending'", (pid,)).rowcount
        row = c.execute("SELECT * FROM pub_payments WHERE id=?", (pid,)).fetchone()
        if changed:
            now = int(time.time())
            u = c.execute("SELECT plan, plan_until FROM pub_users WHERE user_id=?", (row["user_id"],)).fetchone()
            base = u["plan_until"] if (u and u["plan"] == row["plan"] and (u["plan_until"] or 0) > now) else now
            c.execute("INSERT OR IGNORE INTO pub_users(user_id, username, created) VALUES(?,?,?)",
                      (row["user_id"], row["username"], now))
            c.execute("UPDATE pub_users SET plan=?, plan_until=? WHERE user_id=?",
                      (row["plan"], base + PUB_DAYS * 86400, row["user_id"]))
            until = base + PUB_DAYS * 86400
    if not changed:
        await cb.answer("Эта заявка уже обработана.", show_alert=True)
        return await pub_close_admin_msgs(bot, pid, "Уже обработано", cb.message)
    p = PUB_PLANS[row["plan"]]
    await pub_close_admin_msgs(bot, pid, f"✅ Подтверждён ({html.escape(cb.from_user.full_name)})", cb.message)
    if row["wait_mid"]:
        await pub_safe_delete(bot, row["user_id"], row["wait_mid"])
    try:
        await bot.send_message(row["user_id"],
                               f"✅ <b>Платёж подтверждён!</b>\nТариф <b>{p['name']}</b> активен до {pub_date(until)} 🎉",
                               reply_markup=pub_main_kb())
    except Exception:
        pass
    await cb.answer("Готово")


@pub_router.callback_query(F.data.startswith("ppno:"), PUB_ADMIN)
async def pub_pay_no(cb: CallbackQuery, state: FSMContext, bot: Bot):
    pid = int(cb.data.split(":")[1])
    with db() as c:
        st = c.execute("SELECT status FROM pub_payments WHERE id=?", (pid,)).fetchone()
    if not st or st["status"] != "pending":
        await cb.answer("Эта заявка уже обработана.", show_alert=True)
        return await pub_close_admin_msgs(bot, pid, "Уже обработано", cb.message)
    await state.set_state(PubReject.reason)
    await state.update_data(pid=pid, src=(cb.message.chat.id, cb.message.message_id, html.escape(cb.message.caption or "")))
    await cb.message.answer(f"✍️ Напишите <b>причину отказа</b> для платежа #{pid} (её получит пользователь):")
    await cb.answer()


@pub_router.message(PubReject.reason, PUB_ADMIN, F.text)
async def pub_pay_reason(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    pid, reason = d["pid"], m.text.strip()[:500]
    with db() as c:
        changed = c.execute("UPDATE pub_payments SET status='rejected', reason=? WHERE id=? AND status='pending'",
                            (reason, pid)).rowcount
        row = c.execute("SELECT * FROM pub_payments WHERE id=?", (pid,)).fetchone()
    if not changed:
        return await m.answer("Эта заявка уже обработана.")
    if not _pub_pay_msgs.get(pid):
        _pub_pay_msgs[pid] = [d["src"]]
    await pub_close_admin_msgs(bot, pid, f"❌ Отклонён ({html.escape(m.from_user.full_name)})\nПричина: {html.escape(reason)}")
    if row["wait_mid"]:
        await pub_safe_delete(bot, row["user_id"], row["wait_mid"])
    try:
        await bot.send_message(row["user_id"],
                               f"❌ <b>Платёж отклонён.</b>\nПричина: {html.escape(reason)}\n\n"
                               "Вы можете оплатить ещё раз через «💎 Подписки».")
    except Exception:
        pass
    await m.answer("Отказ отправлен пользователю.")


# ── админ ──

@pub_router.message(Command("admin"), PUB_ADMIN)
async def pub_admin(m: Message):
    with db() as c:
        users = c.execute("SELECT COUNT(*) FROM pub_users").fetchone()[0]
        paid = c.execute("SELECT COUNT(*) FROM pub_users WHERE plan!='free' AND plan_until>?", (int(time.time()),)).fetchone()[0]
        posts = c.execute("SELECT COUNT(*) FROM pub_posts").fetchone()[0]
        pend = c.execute("SELECT COUNT(*) FROM pub_payments WHERE status='pending'").fetchone()[0]
    await m.answer(f"📊 Пользователей: {users}\nПлатных: {paid}\nПубликаций: {posts}\nЗаявок на оплату: {pend}\n\n"
                   "Выдать тариф вручную: /give ID standard|pro|free [дней]")


@pub_router.message(Command("give"), PUB_ADMIN)
async def pub_give(m: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[0].isdigit() or parts[1] not in PUB_PLANS:
        return await m.answer("Формат: /give ID standard|pro|free [дней]")
    uid, plan = int(parts[0]), parts[1]
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else PUB_DAYS
    until = 0 if plan == "free" else int(time.time()) + days * 86400
    with db() as c:
        c.execute("INSERT OR IGNORE INTO pub_users(user_id, created) VALUES(?,?)", (uid, int(time.time())))
        c.execute("UPDATE pub_users SET plan=?, plan_until=? WHERE user_id=?", (plan, until, uid))
    await m.answer(f"Готово: {uid} → {PUB_PLANS[plan]['name']}" + (f" до {pub_date(until)}" if until else ""))


async def run_publisher(handle_signals: bool = False) -> None:
    """Запуск второго бота (публикации в каналах). Ошибка тут не останавливает первый бот."""
    global _pub_username
    try:
        pbot = Bot(PUB_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        _pub_username = (await pbot.get_me()).username
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(pub_router)
        try:
            await pbot.set_my_commands([BotCommand(command="start", description="Запустить бота"),
                                        BotCommand(command="cancel", description="Отмена")])
        except Exception as e:
            log.warning("команды второго бота: %s", e)
        await pbot.delete_webhook(drop_pending_updates=True)
        log.info("Бот публикаций @%s запущен", _pub_username)
        with db() as c:
            custom_ids = [r["id"] for r in c.execute("SELECT id FROM pub_bots WHERE status='active'")]
        for cid in custom_ids:
            await pub_custom_start(cid)
        if custom_ids:
            log.info("Личных ботов пользователей запущено: %s", len(_custom))
        try:
            await dp.start_polling(pbot, handle_signals=handle_signals)
        finally:
            for cid in list(_custom):
                await pub_custom_stop(cid)
            await pbot.session.close()
    except TelegramUnauthorizedError:
        log.error("PUBLISH_BOT_TOKEN неверный — бот публикаций не запущен")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("бот публикаций остановился")



async def setup_commands(bot: Bot) -> None:
    """Команды в кнопке «Меню» рядом с полем ввода: у всех — общие, у админов — расширенный список."""
    users = [BotCommand(command="start", description="Запустить бота"),
             BotCommand(command="menu", description="Главное меню")]
    admins = users + [BotCommand(command="admin", description="Админ-панель"),
                      BotCommand(command="setpromo", description="Фото рекламы бота"),
                      BotCommand(command="backup", description="Бэкап базы в GitHub"),
                      BotCommand(command="aicheck", description="Проверка ключей генерации"),
                      BotCommand(command="give", description="Выдать генерации: /give ID КОЛ-ВО"),
                      BotCommand(command="cancel", description="Отмена")]
    try:
        await bot.set_my_commands(users, scope=BotCommandScopeDefault())
        for aid in ADMIN_IDS:
            try:
                await bot.set_my_commands(admins, scope=BotCommandScopeChat(chat_id=aid))
            except Exception as e:
                log.warning("команды для админа %s: %s", aid, e)
        if WEBAPP_URL:  # вместо списка команд — кнопка, открывающая мини-приложение
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="Приложение", web_app=WebAppInfo(url=f"{WEBAPP_URL}/app")))
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        log.warning("не удалось установить команды меню: %s", e)


async def main():
    global BOT_USERNAME
    if not BOT_TOKEN and not PUB_BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (и PUBLISH_BOT_TOKEN)")
    if BOT_TOKEN and not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — админ-панель будет недоступна")
    await gh_restore()
    init_db()
    pub_init_db()
    pub_task = None
    if PUB_BOT_TOKEN:  # второй бот работает параллельно, в этом же процессе
        pub_task = asyncio.create_task(run_publisher(handle_signals=not BOT_TOKEN))
    else:
        log.info("PUBLISH_BOT_TOKEN не задан — бот публикаций выключен")
    task = None
    if GH_ENABLED:
        log.info("GitHub-бэкап включён: %s", GITHUB_REPO)
        task = asyncio.create_task(backup_loop())
    else:
        log.warning("GitHub-бэкап выключен — данные хранятся только в %s", DB_PATH)
    runner = None
    try:
        if BOT_TOKEN:
            bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            BOT_USERNAME = (await bot.get_me()).username
            dp = Dispatcher(storage=MemoryStorage())
            dp.include_router(router)
            await setup_commands(bot)
            await bot.delete_webhook(drop_pending_updates=True)
            log.info("Бот @%s запущен", BOT_USERNAME)
            if WEBAPP_URL:
                from aiohttp import web

                runner = web.AppRunner(make_web_app(bot))
                await runner.setup()
                await web.TCPSite(runner, "0.0.0.0", PORT).start()
                log.info("Мини-приложение: %s (порт %s)", WEBAPP_URL, PORT)
            else:
                log.warning("WEBAPP_URL не задан — промт выдаётся в чат, без мини-приложения")
            await dp.start_polling(bot)
        else:
            await pub_task
    finally:
        if runner:
            await runner.cleanup()
        if pub_task:
            pub_task.cancel()
        if task:
            task.cancel()
            await gh_backup()  # финальное сохранение при остановке


if __name__ == "__main__":
    asyncio.run(main())
