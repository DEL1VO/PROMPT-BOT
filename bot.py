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
import html
import logging
import os
import sqlite3
import time

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
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
            CREATE TABLE IF NOT EXISTS claims(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                post_id INTEGER,
                ts INTEGER
            );
            """
        )


# ───────────────────────────── Состояния ─────────────────────────────

class NewPost(StatesGroup):
    content = State()
    prompt = State()
    subs = State()
    channel = State()


class AddChannel(StatesGroup):
    wait = State()


class AddBot(StatesGroup):
    wait = State()


# ───────────────────────────── Вспомогательное ─────────────────────────────

def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новая публикация", callback_data="adm:newpost")
    kb.button(text="📋 Публикации", callback_data="adm:posts")
    kb.button(text="📢 Каналы и боты для подписки", callback_data="adm:subs")
    kb.button(text="📊 Статистика", callback_data="adm:stats")
    kb.adjust(1)
    return kb.as_markup()


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu")]]
    )


def save_user(m: Message) -> None:
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_seen) VALUES(?,?,?)",
            (m.from_user.id, m.from_user.username, int(time.time())),
        )


async def is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        cm = await bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        log.warning("get_chat_member(%s) failed: %s", chat_id, e)
        return False
    if cm.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        return False
    if cm.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(cm, "is_member", True))
    return True


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


async def process(bot: Bot, user_id: int, post_id: int, bots_confirmed: bool = False) -> None:
    """Проверяет подписки публикации и выдаёт её промт (или просит подписаться)."""
    with db() as c:
        post = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        await bot.send_message(user_id, "😔 Эта публикация не найдена или была удалена.")
        return

    subs = post_subs(post_id)
    bots = [s for s in subs if s["kind"] == "bot"]
    missing = await missing_channels(bot, user_id, post_id)

    if missing or (bots and not bots_confirmed):
        kb = InlineKeyboardBuilder()
        for s in missing:
            kb.button(text=f"📢 {s['title']}", url=s["link"])
        for s in bots:
            kb.button(text=f"🤖 {s['title']}", url=s["link"])
        kb.button(text="✅ Готово", callback_data=f"check:{post_id}")
        kb.adjust(1)
        what = []
        if missing:
            what.append("подпишитесь на каналы")
        if bots:
            what.append("запустите ботов")
        await bot.send_message(
            user_id,
            f"Чтобы получить промт, {' и '.join(what)} ниже, затем нажмите «✅ Готово».",
            reply_markup=kb.as_markup(),
        )
        return

    with db() as c:
        c.execute("INSERT INTO claims(user_id, post_id, ts) VALUES(?,?,?)", (user_id, post_id, int(time.time())))
    await send_prompt(bot, user_id, post_id, post["prompt"])


# ───────────────────────────── Пользователи ─────────────────────────────

@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    save_user(m)
    arg = (command.args or "").strip()

    if arg.startswith("p") and arg[1:].isdigit():
        await process(bot, m.from_user.id, int(arg[1:]))
        return

    if m.from_user.id in ADMIN_IDS:
        await m.answer("👋 Админ-панель:", reply_markup=admin_menu())
    else:
        await m.answer("Привет! Нажмите кнопку «Получить промт» под публикацией в канале 💛")


@router.callback_query(F.data.startswith("check:"))
async def cb_check(cb: CallbackQuery, bot: Bot):
    post_id = int(cb.data.split(":")[1])
    if await missing_channels(bot, cb.from_user.id, post_id):
        await cb.answer("Вы ещё не подписались на все каналы 🙈", show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await process(bot, cb.from_user.id, post_id, bots_confirmed=True)


# ───────────────────────────── Админка ─────────────────────────────

@router.message(Command("admin"), ADMIN)
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("👋 Админ-панель:", reply_markup=admin_menu())


@router.message(Command("cancel"), ADMIN)
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=admin_menu())


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
    await cb.message.edit_text(
        f"📊 <b>Статистика</b>\n\nПользователей: {users}\nПубликаций: {posts}\n"
        f"Выдач промта: {claims} (уникальных людей: {uniq})",
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
        lines.append(f"#{r['id']} → {r['channel_id']} · подписок: {r['s']} · выдач: {r['n']}")
        kb.button(text=f"🗑 #{r['id']}", callback_data=f"postdel:{r['id']}")
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    kb.adjust(*([3] * ((len(rows) + 2) // 3)), 1)
    await cb.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
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
        return await m.answer("Нужен текст или фото с подписью.")
    await state.set_state(NewPost.prompt)
    await m.answer(
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
            return await m.answer("Файл слишком большой (максимум 1 МБ).")
        buf = await bot.download(m.document)
        raw = buf.read()
        try:
            prompt = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                prompt = raw.decode("cp1251")
            except UnicodeDecodeError:
                return await m.answer("Не смог прочитать файл. Пришлите текстовый файл .txt в UTF-8.")
        prompt = prompt.strip()
        if not prompt:
            return await m.answer("Файл пустой.")
    else:
        prompt = m.text
    await state.update_data(prompt=prompt, chosen=[])
    with db() as c:
        catalog = c.execute("SELECT * FROM subs ORDER BY id").fetchall()
    if not catalog:
        await state.set_state(NewPost.channel)
        return await m.answer(
            "3/4. В каталоге нет каналов/ботов для подписки — эта публикация будет без подписки "
            "(добавить их можно в меню «Каналы и боты»).\n\n"
            "4/4. Куда публиковать? Пришлите <code>@username</code> канала, ID или перешлите сообщение из канала."
        )
    await state.set_state(NewPost.subs)
    await m.answer(
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
        return await m.answer("Не понял. Пришлите @username, ID или перешлите сообщение из канала.")
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
        return await m.answer(f"❌ Не удалось опубликовать: {html.escape(str(e))}\nПроверьте, что бот — админ канала.")

    with db() as c:
        c.execute("UPDATE posts SET message_id=?, channel_id=? WHERE id=?", (sent.message_id, str(sent.chat.id), post_id))
    await state.clear()
    await m.answer(f"✅ Опубликовано (публикация #{post_id}).", reply_markup=admin_menu())


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
        lines.append(f"{icon} {html.escape(r['title'])}")
        kb.button(text=f"🗑 {r['title'][:28]}", callback_data=f"subdel:{r['id']}")
    kb.button(text="➕ Канал", callback_data="subadd:channel")
    kb.button(text="➕ Бот", callback_data="subadd:bot")
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    kb.adjust(*([1] * len(rows)), 2, 1)
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
        return await m.answer("Не понял. Пришлите @username, ID или пересланное сообщение.")
    try:
        chat = await bot.get_chat(ref)
        me = await bot.get_chat_member(chat.id, bot.id)
        if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            return await m.answer("Сделайте бота админом канала и пришлите канал ещё раз.")
        if chat.username:
            link = f"https://t.me/{chat.username}"
        else:
            link = (await bot.create_chat_invite_link(chat.id)).invite_link
    except Exception as e:
        return await m.answer(f"❌ Не получилось: {html.escape(str(e))}")
    with db() as c:
        c.execute("INSERT INTO subs(kind, chat_id, title, link) VALUES('channel',?,?,?)", (chat.id, chat.title or str(chat.id), link))
    await state.clear()
    await m.answer(f"✅ Канал «{html.escape(chat.title or '')}» добавлен.", reply_markup=admin_menu())


@router.callback_query(F.data == "subadd:bot", ADMIN)
async def cb_subadd_bot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddBot.wait)
    await cb.message.edit_text(
        "Пришлите <code>@username</code> бота или ссылку на него (можно с параметром, "
        "например <code>https://t.me/mybot?start=abc</code>).\n/cancel — отмена"
    )
    await cb.answer()


@router.message(AddBot.wait, ADMIN, F.text)
async def add_bot(m: Message, state: FSMContext):
    t = m.text.strip()
    if t.startswith("@"):
        username, link = t[1:], f"https://t.me/{t[1:]}"
    elif "t.me/" in t:
        link = t if t.startswith("http") else "https://" + t
        username = t.split("t.me/")[1].split("?")[0].strip("/")
    else:
        return await m.answer("Пришлите @username или ссылку вида https://t.me/mybot")
    with db() as c:
        c.execute("INSERT INTO subs(kind, chat_id, title, link) VALUES('bot',NULL,?,?)", (f"@{username}", link))
    await state.clear()
    await m.answer(f"✅ Бот @{html.escape(username)} добавлен.", reply_markup=admin_menu())


# ───────────────────────────── Запуск ─────────────────────────────

async def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — админ-панель будет недоступна")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот @%s запущен", BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
