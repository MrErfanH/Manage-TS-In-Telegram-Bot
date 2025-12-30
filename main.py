import os
import re
import time
import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, ChatMemberUpdated
from aiogram.enums import ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder
load_dotenv("config.env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TS_HOST = os.getenv("TS_HOST")
TS_QUERY_PORT = int(os.getenv("TS_QUERY_PORT", "10011"))
TS_USER = os.getenv("TS_USER")
TS_PASS = os.getenv("TS_PASS")
TS_VSID = int(os.getenv("TS_VSID", "1"))
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", str(GROUP_ID)))
ADMIN_IDS = {int(x.strip()) for x in (os.getenv("ADMIN_IDS", "")).split(",") if x.strip().isdigit()}
OG_GROUP_NAME = os.getenv("OG_GROUP_NAME", "-OG-")
HEADER_TS = ""
TS_PUBLIC_ADDR = ""
DB_PATH = os.getenv("DB_PATH", "bot.db")
FOOTER = "\n\n<code>Powered by CodeNox™</code>"
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
pending_poke: Dict[int, int] = {}
pending_msg: Dict[int, int] = {}
pending_verify: Dict[int, int] = {}
pending_manage_action: Dict[int, Tuple[int, str]] = {} 
pending_ban_reason_edit: Dict[int, int] = {}       
pending_broadcast: Dict[int, bool] = {}            
last_user_action: Dict[int, float] = {}
USER_COOLDOWN = 6.0
ADMIN_COOLDOWN = 4.0
servergroup_cache: Dict[int, str] = {}
servergroup_cache_at: float = 0.0
SERVERGROUP_CACHE_TTL = 300.0
og_sgid_cache: Optional[int] = None
def _db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn
async def db_init():
    def _init():
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            first_seen TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            username TEXT,
            chat_type TEXT,
            is_active INTEGER DEFAULT 1,
            last_seen TEXT
        )
        """)
        conn.commit()
        conn.close()
    await asyncio.to_thread(_init)
async def db_save_user(tg_id: int, full_name: str, username: Optional[str]):
    now = datetime.utcnow().isoformat(timespec="seconds")
    def _save():
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,))
        exists = cur.fetchone() is not None
        if not exists:
            cur.execute(
                "INSERT INTO users (tg_id, full_name, username, first_seen) VALUES (?, ?, ?, ?)",
                (tg_id, full_name or "", username or "", now)
            )
        else:
            cur.execute(
                "UPDATE users SET full_name=?, username=? WHERE tg_id=?",
                (full_name or "", username or "", tg_id)
            )
        conn.commit()
        conn.close()
    await asyncio.to_thread(_save)
async def db_upsert_chat(chat_id: int, title: str, username: Optional[str], chat_type: str, is_active: bool):
    now = datetime.utcnow().isoformat(timespec="seconds")
    def _upsert():
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO chats (chat_id, title, username, chat_type, is_active, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            username=excluded.username,
            chat_type=excluded.chat_type,
            is_active=excluded.is_active,
            last_seen=excluded.last_seen
        """, (chat_id, title or "", username or "", chat_type or "", 1 if is_active else 0, now))
        conn.commit()
        conn.close()
    await asyncio.to_thread(_upsert)
async def db_get_broadcast_targets() -> Tuple[List[int], List[int]]:
    def _get():
        conn = _db_connect()
        cur = conn.cursor()

        cur.execute("SELECT tg_id FROM users")
        user_ids = [int(r[0]) for r in cur.fetchall()]

        cur.execute("SELECT chat_id FROM chats WHERE is_active=1")
        chat_ids = [int(r[0]) for r in cur.fetchall()]

        conn.close()
        return user_ids, chat_ids

    return await asyncio.to_thread(_get)


def is_allowed_group_chat(obj) -> bool:
    try:
        chat = obj.chat if hasattr(obj, "chat") else obj.message.chat
        return chat and chat.id == GROUP_ID
    except Exception:
        return False

async def tg_is_in_main_group(user_id: int) -> bool:
    if GROUP_ID == 0:
        return False
    try:
        member = await bot.get_chat_member(GROUP_ID, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.RESTRICTED,
        )
    except Exception:
        return False

def is_whitelisted_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def cooldown_ok(user_id: int, is_admin: bool) -> bool:
    now = time.time()
    last = last_user_action.get(user_id, 0.0)
    limit = ADMIN_COOLDOWN if is_admin else USER_COOLDOWN
    if now - last < limit:
        return False
    last_user_action[user_id] = now
    return True


def ts_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
         .replace("/", "\\/")
         .replace(" ", "\\s")
         .replace("|", "\\p")
         .replace("\n", "\\n")
         .replace("\r", "")
         .replace("\t", "\\t")
         .replace("=", "\\=")
    )

def ts_unescape(s: str) -> str:
    return (
        s.replace("\\s", " ")
         .replace("\\p", "|")
         .replace("\\/", "/")
         .replace("\\n", "\n")
         .replace("\\t", "\t")
         .replace("\\\\", "\\")
         .replace("\\=", "=")
    )

def is_ok(resp: str) -> bool:
    return "error id=0" in resp

async def read_banner(reader: asyncio.StreamReader, max_lines: int = 20, timeout: float = 1.2) -> None:
    for _ in range(max_lines):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if not line:
            break
        if b"error id=" in line:
            break

async def read_until_error(reader: asyncio.StreamReader, timeout: float = 6.0) -> str:
    buf: List[str] = []
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not line:
            break
        text = line.decode("utf-8", errors="ignore")
        buf.append(text)
        if "error id=" in text:
            break
    return "".join(buf)

async def send_cmd(writer: asyncio.StreamWriter, reader: asyncio.StreamReader, cmd: str, timeout: float = 6.0) -> str:
    writer.write((cmd + "\n").encode("utf-8"))
    await writer.drain()
    return await read_until_error(reader, timeout=timeout)

@dataclass
class TSConnTimings:
    connect_ms: float
    login_ms: float
    use_ms: float

async def ts_connect() -> Tuple[asyncio.StreamReader, asyncio.StreamWriter, TSConnTimings]:
    t0 = time.perf_counter()
    reader, writer = await asyncio.open_connection(TS_HOST, TS_QUERY_PORT)
    connect_ms = (time.perf_counter() - t0) * 1000

    await read_banner(reader)

    t1 = time.perf_counter()
    login_cmd = f"login client_login_name={ts_escape(TS_USER)} client_login_password={ts_escape(TS_PASS)}"
    login_resp = await send_cmd(writer, reader, login_cmd)
    login_ms = (time.perf_counter() - t1) * 1000
    if not is_ok(login_resp):
        raise RuntimeError(f"LOGIN FAILED:\n{login_resp}")

    t2 = time.perf_counter()
    use_resp = await send_cmd(writer, reader, f"use sid={TS_VSID}")
    use_ms = (time.perf_counter() - t2) * 1000
    if not is_ok(use_resp):
        raise RuntimeError(f"USE FAILED:\n{use_resp}")

    return reader, writer, TSConnTimings(connect_ms=connect_ms, login_ms=login_ms, use_ms=use_ms)

async def ts_close(writer: asyncio.StreamWriter):
    try:
        writer.write(b"quit\n")
        await writer.drain()
    except Exception:
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


def parse_kv_blocks(payload: str) -> List[Dict[str, str]]:
    payload = payload.strip()
    if not payload:
        return []
    blocks = payload.split("|")
    out = []
    for b in blocks:
        data: Dict[str, str] = {}
        for part in b.strip().split():
            if "=" in part:
                k, v = part.split("=", 1)
                data[k] = v
        out.append(data)
    return out

def ms_to_hms(ms: int) -> str:
    s = ms // 1000
    h = s // 3600
    m = (s % 3600) // 60
    ss = s % 60
    return f"{h:02d}:{m:02d}:{ss:02d}"

def tg_display_name(user) -> str:
    name = (user.full_name or "").strip()
    if not name and user.username:
        name = user.username
    if not name:
        name = f"user{user.id}"
    name = name.replace("\n", " ").replace("\r", " ").strip()
    return name[:25]


async def fetch_clientlist_with_times() -> Tuple[List[Dict[str, str]], Dict[str, float]]:
    reader, writer, t = await ts_connect()
    timings = {
        "connect_ms": t.connect_ms,
        "login_ms": t.login_ms,
        "use_ms": t.use_ms,
    }
    try:
        t3 = time.perf_counter()
        resp = await send_cmd(writer, reader, "clientlist -times -groups -uid")
        timings["clientlist_ms"] = (time.perf_counter() - t3) * 1000

        if not is_ok(resp):
            raise RuntimeError(f"CLIENTLIST FAILED:\n{resp}")

        payload = resp.split("error", 1)[0]
        items = parse_kv_blocks(payload)
        users = [it for it in items if it.get("client_type") == "0"]
        timings["total_ms"] = timings["connect_ms"] + timings["login_ms"] + timings["use_ms"] + timings["clientlist_ms"]
        return users, timings
    finally:
        await ts_close(writer)

async def fetch_clientinfo(clid: int) -> Dict[str, str]:
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"clientinfo clid={clid}")
        if not is_ok(resp):
            raise RuntimeError(f"CLIENTINFO FAILED:\n{resp}")
        payload = resp.split("error", 1)[0]
        items = parse_kv_blocks(payload)
        return items[0] if items else {}
    finally:
        await ts_close(writer)

async def fetch_channel_name(cid: int) -> str:
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"channelinfo cid={cid}")
        if not is_ok(resp):
            return "Unknown"
        payload = resp.split("error", 1)[0]
        items = parse_kv_blocks(payload)
        if not items:
            return "Unknown"
        return ts_unescape(items[0].get("channel_name", "Unknown"))
    finally:
        await ts_close(writer)

async def refresh_servergroup_cache() -> None:
    global servergroup_cache, servergroup_cache_at, og_sgid_cache

    now = time.time()
    if servergroup_cache and (now - servergroup_cache_at) < SERVERGROUP_CACHE_TTL:
        return

    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, "servergrouplist")
        if not is_ok(resp):
            raise RuntimeError(f"SERVERGROUPLIST FAILED:\n{resp}")
        payload = resp.split("error", 1)[0]
        items = parse_kv_blocks(payload)
        cache = {}
        og = None
        for it in items:
            try:
                sgid = int(it.get("sgid", "0"))
            except Exception:
                continue
            name = ts_unescape(it.get("name", ""))
            cache[sgid] = name
            if name == OG_GROUP_NAME:
                og = sgid
        servergroup_cache = cache
        servergroup_cache_at = now
        og_sgid_cache = og
    finally:
        await ts_close(writer)

async def poke(clid: int, from_user, text: str) -> None:
    from_name = tg_display_name(from_user)
    final_msg = f"TelPoke-{from_name} : {text}"
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"clientpoke clid={clid} msg={ts_escape(final_msg)}")
        if not is_ok(resp):
            raise RuntimeError(f"POKE FAILED:\n{resp}")
    finally:
        await ts_close(writer)

async def private_message(clid: int, from_user, text: str) -> None:
    from_name = tg_display_name(from_user)
    final_msg = f"TelMsg-{from_name} : {text}"
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"sendtextmessage targetmode=1 target={clid} msg={ts_escape(final_msg)}")
        if not is_ok(resp):
            raise RuntimeError(f"PM FAILED:\n{resp}")
    finally:
        await ts_close(writer)

async def kick_server(clid: int, from_user, reason: str) -> None:
    from_name = tg_display_name(from_user)
    final_reason = f"TelKickServer-{from_name} : {reason}"
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"clientkick clid={clid} reasonid=5 reasonmsg={ts_escape(final_reason)}")
        if not is_ok(resp):
            raise RuntimeError(f"KICK SERVER FAILED:\n{resp}")
    finally:
        await ts_close(writer)

async def kick_channel(clid: int, from_user, reason: str) -> None:
    from_name = tg_display_name(from_user)
    final_reason = f"TelKickChannel-{from_name} : {reason}"
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"clientkick clid={clid} reasonid=4 reasonmsg={ts_escape(final_reason)}")
        if not is_ok(resp):
            raise RuntimeError(f"KICK CHANNEL FAILED:\n{resp}")
    finally:
        await ts_close(writer)

async def ban_temp(clid: int, from_user, seconds: int, reason: str) -> None:
    from_name = tg_display_name(from_user)
    final_reason = f"TelBanTemp-{from_name} : {reason}"
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"banclient clid={clid} time={seconds} banreason={ts_escape(final_reason)}")
        if not is_ok(resp):
            raise RuntimeError(f"BAN TEMP FAILED:\n{resp}")
    finally:
        await ts_close(writer)

async def ban_perm(clid: int, from_user, reason: str) -> None:
    from_name = tg_display_name(from_user)
    final_reason = f"TelBanPerm-{from_name} : {reason}"
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"banclient clid={clid} time=0 banreason={ts_escape(final_reason)}")
        if not is_ok(resp):
            raise RuntimeError(f"BAN PERM FAILED:\n{resp}")
    finally:
        await ts_close(writer)

async def add_server_group(clid: int, sgid: int) -> None:
    info = await fetch_clientinfo(clid)
    cldbid = info.get("client_database_id")
    if not cldbid:
        raise RuntimeError("Could not get client_database_id.")
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"servergroupaddclient sgid={sgid} cldbid={cldbid}")
        if not is_ok(resp):
            raise RuntimeError(f"ADD SERVER GROUP FAILED:\n{resp}")
    finally:
        await ts_close(writer)

async def remove_server_group(clid: int, sgid: int) -> None:
    info = await fetch_clientinfo(clid)
    cldbid = info.get("client_database_id")
    if not cldbid:
        raise RuntimeError("Could not get client_database_id.")
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"servergroupdelclient sgid={sgid} cldbid={cldbid}")
        if not is_ok(resp):
            raise RuntimeError(f"REMOVE SERVER GROUP FAILED:\n{resp}")
    finally:
        await ts_close(writer)

async def set_client_description(clid: int, desc: str) -> None:
    info = await fetch_clientinfo(clid)
    cldbid = info.get("client_database_id")

    reader, writer, _ = await ts_connect()
    try:
        await send_cmd(writer, reader, f"clientedit clid={clid} client_description={ts_escape(desc)}")
        if cldbid:
            await send_cmd(writer, reader, f"clientdbedit cldbid={cldbid} client_description={ts_escape(desc)}")
    finally:
        await ts_close(writer)


async def verify_add_og_and_description_by_text(clid: int, verify_text: str) -> None:
    await refresh_servergroup_cache()
    if og_sgid_cache is None:
        raise RuntimeError(f"OG group not found by name: {OG_GROUP_NAME} (check OG_GROUP_NAME)")

    info = await fetch_clientinfo(clid)
    cldbid = info.get("client_database_id")
    if not cldbid:
        raise RuntimeError("Could not get client_database_id from clientinfo.")

    vt = (verify_text or "").strip() or "Verified"
    desc = f"Verify By NoxBot || {vt}"

    reader, writer, _ = await ts_connect()
    try:
        r1 = await send_cmd(writer, reader, f"servergroupaddclient sgid={og_sgid_cache} cldbid={cldbid}")
        if not is_ok(r1):
            raise RuntimeError(f"ADD OG GROUP FAILED:\n{r1}")
    finally:
        await ts_close(writer)

    await set_client_description(clid, desc)


async def ts_banlist() -> List[Dict[str, str]]:
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, "banlist")
        if not is_ok(resp):
            raise RuntimeError(f"BANLIST FAILED:\n{resp}")
        payload = resp.split("error", 1)[0].strip()
        return parse_kv_blocks(payload)
    finally:
        await ts_close(writer)

async def ts_unban(banid: int) -> None:
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"bandel banid={banid}")
        if not is_ok(resp):
            raise RuntimeError(f"UNBAN FAILED:\n{resp}")
    finally:
        await ts_close(writer)

async def ts_banedit_reason(banid: int, reason: str) -> None:
    reader, writer, _ = await ts_connect()
    try:
        resp = await send_cmd(writer, reader, f"banedit banid={banid} banreason={ts_escape(reason)}")
        if not is_ok(resp):
            raise RuntimeError(f"BANEDIT FAILED:\n{resp}")
    finally:
        await ts_close(writer)



def kb_ts_list(items: List[Dict[str, str]], show_verify: bool) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for it in items:
        clid = int(it["clid"])
        nick = ts_unescape(it.get("client_nickname", "User"))
        kb.button(text=f"🔔 پوک {nick}", callback_data=f"ts:poke:{clid}")
        kb.button(text=f"✉️ پیام {nick}", callback_data=f"ts:msg:{clid}")
        kb.button(text=f"ℹ️ مشخصات {nick}", callback_data=f"ts:more:{clid}")
        if show_verify:
            kb.button(text=f"✅ Verify {nick}", callback_data=f"ts:verify:{clid}")
    kb.button(text="🔄 رفرش", callback_data="ts:refresh")
    kb.adjust(2)
    return kb

def kb_manage_main() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 مشاهده کاربران آنلاین", callback_data="mg:users")
    kb.button(text="📛 Ban List", callback_data="mg:bans")
    kb.button(text="📣 پیام دادن به همه", callback_data="mg:broadcast")
    kb.adjust(1)
    return kb

def kb_manage_pick_user(items: List[Dict[str, str]]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for it in items:
        clid = int(it["clid"])
        nick = ts_unescape(it.get("client_nickname", "User"))
        kb.button(text=f"👤 {nick}", callback_data=f"mg:user:{clid}")
    kb.button(text="⬅️ Back", callback_data="mg:back_main")
    kb.button(text="🔄 Refresh", callback_data="mg:refresh_users")
    kb.adjust(2)
    return kb

def kb_manage_actions(clid: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="👢 Kick Server", callback_data=f"mg:act:kick_server:{clid}")
    kb.button(text="👋 Kick Channel", callback_data=f"mg:act:kick_channel:{clid}")
    kb.button(text="⛔ Ban Permanent", callback_data=f"mg:act:ban_perm:{clid}")
    kb.button(text="⏳ Ban Temp", callback_data=f"mg:act:ban_temp:{clid}")
    kb.button(text="➕ Add Server Group", callback_data=f"mg:act:add_group:{clid}")
    kb.button(text="➖ Remove Server Group", callback_data=f"mg:act:remove_group:{clid}")
    kb.button(text="🌐 ShowIP", callback_data=f"mg:act:show_ip:{clid}")
    kb.button(text="⬅️ Back", callback_data="mg:back_users")
    kb.adjust(2)
    return kb

def kb_ban_list(bans: List[Dict[str, str]], limit: int = 15) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for it in bans[:limit]:
        banid = int(it.get("banid", "0") or "0")
        name = ts_unescape(it.get("name", "") or it.get("uid", "") or it.get("ip", "") or f"banid={banid}")
        if len(name) > 26:
            name = name[:26] + "…"
        kb.button(text=f"📛 {name}", callback_data=f"bn:open:{banid}")
    kb.button(text="⬅️ Back", callback_data="mg:back_main")
    kb.adjust(1)
    return kb

def kb_ban_actions(banid: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Unban", callback_data=f"bn:unban:{banid}")
    kb.button(text="✍️ Change Reason", callback_data=f"bn:reason:{banid}")
    kb.button(text="⬅️ Back", callback_data="mg:bans")
    kb.adjust(2)
    return kb


@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    new_status = update.new_chat_member.status
    chat = update.chat
    is_active = new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)

    await db_upsert_chat(
        chat_id=chat.id,
        title=getattr(chat, "title", "") or "",
        username=getattr(chat, "username", "") or "",
        chat_type=chat.type,
        is_active=is_active
    )


# -------------------------
@dp.message(F.text.regexp(r"^/start(\s|$)"))
async def cmd_start(message: Message):

    await db_save_user(
        tg_id=message.from_user.id,
        full_name=message.from_user.full_name or "",
        username=message.from_user.username or ""
    )


    if not await tg_is_in_main_group(message.from_user.id):
        return await message.reply("❌ برای استفاده از ربات باید عضو گروه اصلی باشید." + FOOTER, parse_mode="HTML")

    await message.reply(
        "✅ خوش آمدید.\nداخل گروه اصلی: /ts\nادمین‌ها در پیوی: /manage" + FOOTER,
        parse_mode="HTML"
    )

@dp.message(F.text.regexp(r"^/ts(\s|$)"))
async def cmd_ts(message: Message):
    if not is_allowed_group_chat(message):
        return
    if not await tg_is_in_main_group(message.from_user.id):
        return await message.reply("❌ برای استفاده از این ربات باید عضو گروه اصلی باشید." + FOOTER, parse_mode="HTML")

    try:
        items, t = await fetch_clientlist_with_times()

        admin_for_verify = is_whitelisted_admin(message.from_user.id)
        kb = kb_ts_list(items, show_verify=admin_for_verify).as_markup()

        header = f"<b>{HEADER_TS}</b>\n\n"
        footer_time = (
            f"\n\n⏱ connect {t['connect_ms']:.0f}ms | login {t['login_ms']:.0f}ms | use {t['use_ms']:.0f}ms | "
            f"list {t['clientlist_ms']:.0f}ms | total {t['total_ms']:.0f}ms"
        )

        if not items:
            return await message.reply(header + "👥 الان کسی آنلاین نیست." + footer_time + FOOTER, parse_mode="HTML")

        lines = []
        for it in items:
            nick = ts_unescape(it.get("client_nickname", "User"))
            ctime = int(it.get("connection_connected_time", "0"))
            lines.append(f"• {nick}  ⏱ {ms_to_hms(ctime)}")

        await message.reply(
            header + "👥 آنلاین‌ها:\n" + "\n".join(lines) + footer_time + FOOTER,
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        err = str(e)
        if len(err) > 600:
            err = err[:600] + "…"
        await message.reply("❌ خطا در گرفتن لیست.\n🔎 " + err + FOOTER, parse_mode="HTML")

@dp.message(F.text.regexp(r"^/manage(\s|$)"))
async def cmd_manage(message: Message):
    if message.chat.type != "private":
        return
    if not await tg_is_in_main_group(message.from_user.id):
        return await message.reply("❌ برای استفاده از ربات باید عضو گروه اصلی باشید." + FOOTER, parse_mode="HTML")
    if not is_whitelisted_admin(message.from_user.id):
        return await message.reply("❌ دسترسی مدیریت ندارید." + FOOTER, parse_mode="HTML")

    kb = kb_manage_main().as_markup()
    await message.reply("🛠 پنل مدیریت:" + FOOTER, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "ts:refresh")
async def cb_ts_refresh(call: CallbackQuery):
    if not is_allowed_group_chat(call):
        return await call.answer("نامعتبر", show_alert=True)
    if not await tg_is_in_main_group(call.from_user.id):
        return await call.answer("عضو گروه نیستید.", show_alert=True)

    try:
        items, t = await fetch_clientlist_with_times()
        admin_for_verify = is_whitelisted_admin(call.from_user.id)
        kb = kb_ts_list(items, show_verify=admin_for_verify).as_markup()

        header = f"<b>{HEADER_TS}</b>\n\n"
        footer_time = (
            f"\n\n⏱ connect {t['connect_ms']:.0f}ms | login {t['login_ms']:.0f}ms | use {t['use_ms']:.0f}ms | "
            f"list {t['clientlist_ms']:.0f}ms | total {t['total_ms']:.0f}ms"
        )

        if not items:
            await call.message.edit_text(header + "👥 الان کسی آنلاین نیست." + footer_time + FOOTER, parse_mode="HTML")
            return await call.answer("رفرش شد ✅")

        lines = []
        for it in items:
            nick = ts_unescape(it.get("client_nickname", "User"))
            ctime = int(it.get("connection_connected_time", "0"))
            lines.append(f"• {nick}  ⏱ {ms_to_hms(ctime)}")

        await call.message.edit_text(
            header + "👥 آنلاین‌ها:\n" + "\n".join(lines) + footer_time + FOOTER,
            reply_markup=kb,
            parse_mode="HTML"
        )
        await call.answer("رفرش شد ✅")
    except Exception as e:
        await call.answer("خطا: " + str(e)[:200], show_alert=True)

@dp.callback_query(F.data.startswith("ts:poke:"))
async def cb_ts_poke(call: CallbackQuery):
    if not is_allowed_group_chat(call):
        return await call.answer("نامعتبر", show_alert=True)
    if not await tg_is_in_main_group(call.from_user.id):
        return await call.answer("عضو گروه نیستید.", show_alert=True)
    if not cooldown_ok(call.from_user.id, is_admin=is_whitelisted_admin(call.from_user.id)):
        return await call.answer("کمی صبر کن…", show_alert=True)

    clid = int(call.data.split(":")[-1])
    pending_poke[call.from_user.id] = clid
    await call.answer()
    await call.message.reply("متن پوک رو بفرست:" + FOOTER, parse_mode="HTML")

@dp.callback_query(F.data.startswith("ts:msg:"))
async def cb_ts_msg(call: CallbackQuery):
    if not is_allowed_group_chat(call):
        return await call.answer("نامعتبر", show_alert=True)
    if not await tg_is_in_main_group(call.from_user.id):
        return await call.answer("عضو گروه نیستید.", show_alert=True)
    if not cooldown_ok(call.from_user.id, is_admin=is_whitelisted_admin(call.from_user.id)):
        return await call.answer("کمی صبر کن…", show_alert=True)

    clid = int(call.data.split(":")[-1])
    pending_msg[call.from_user.id] = clid
    await call.answer()
    await call.message.reply("متن پیام خصوصی رو بفرست:" + FOOTER, parse_mode="HTML")

@dp.callback_query(F.data.startswith("ts:more:"))
async def cb_ts_more(call: CallbackQuery):
    if not is_allowed_group_chat(call):
        return await call.answer("نامعتبر", show_alert=True)
    if not await tg_is_in_main_group(call.from_user.id):
        return await call.answer("عضو گروه نیستید.", show_alert=True)

    clid = int(call.data.split(":")[-1])
    try:
        await refresh_servergroup_cache()
        info = await fetch_clientinfo(clid)

        nick = ts_unescape(info.get("client_nickname", "User"))
        desc = ts_unescape(info.get("client_description", "") or "—")

        cid = int(info.get("cid", "0") or "0")
        ch_name = await fetch_channel_name(cid) if cid else "Unknown"

        connected_ms = int(info.get("connection_connected_time", "0") or "0")
        conn_time = ms_to_hms(connected_ms)

        sgids_raw = info.get("client_servergroups", "")
        sg_lines = []
        if sgids_raw:
            for s in sgids_raw.split(","):
                try:
                    sgid = int(s)
                    sg_name = servergroup_cache.get(sgid, str(sgid))
                    sg_lines.append(f"• {sg_name}")
                except Exception:
                    pass

        groups_block = "\n".join(sg_lines) if sg_lines else "—"

        text = (
            f"ℹ️ <b>جزئیات کاربر</b>\n"
            f"• Name: <b>{nick}</b>\n"
            f"• Connected: <b>{conn_time}</b>\n"
            f"• Channel: <b>{ch_name}</b>\n"
            f"• Client Description: {desc}\n\n"
            f"📌 <b>Server Groups</b>\n{groups_block}\n"
            f"\n• clid: <code>{clid}</code>"
            f"{FOOTER}"
        )
        await call.answer()
        await call.message.reply(text, parse_mode="HTML")
    except Exception as e:
        await call.answer("خطا", show_alert=True)
        await call.message.reply("❌ جزئیات ناموفق:\n" + str(e) + FOOTER, parse_mode="HTML")

@dp.callback_query(F.data.startswith("ts:verify:"))
async def cb_ts_verify(call: CallbackQuery):
    if not is_allowed_group_chat(call):
        return await call.answer("نامعتبر", show_alert=True)
    if not is_whitelisted_admin(call.from_user.id):
        return await call.answer("دسترسی ندارید.", show_alert=True)
    if not await tg_is_in_main_group(call.from_user.id):
        return await call.answer("عضو گروه نیستید.", show_alert=True)

    clid = int(call.data.split(":")[-1])
    pending_verify[call.from_user.id] = clid
    await call.answer()
    await call.message.reply(
        "🧾 متن Verify رو بفرست (هرچی بنویسی Verify میشه و همون متن تو Description ست میشه):" + FOOTER,
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("mg:"))
async def cb_manage(call: CallbackQuery):
    if call.message.chat.type != "private":
        return await call.answer("فقط در پیوی.", show_alert=True)
    if not await tg_is_in_main_group(call.from_user.id):
        return await call.answer("عضو گروه نیستید.", show_alert=True)
    if not is_whitelisted_admin(call.from_user.id):
        return await call.answer("دسترسی ندارید.", show_alert=True)

    if call.data == "mg:back_main":
        kb = kb_manage_main().as_markup()
        await call.message.edit_text("🛠 پنل مدیریت:" + FOOTER, reply_markup=kb, parse_mode="HTML")
        return await call.answer("✅")

    if call.data == "mg:users":
        try:
            items, _ = await fetch_clientlist_with_times()
            kb = kb_manage_pick_user(items).as_markup()
            await call.message.edit_text("👥 انتخاب کاربر آنلاین:" + FOOTER, reply_markup=kb, parse_mode="HTML")
            return await call.answer("✅")
        except Exception as e:
            return await call.answer("خطا: " + str(e)[:200], show_alert=True)

    if call.data == "mg:refresh_users":
        try:
            items, _ = await fetch_clientlist_with_times()
            kb = kb_manage_pick_user(items).as_markup()
            await call.message.edit_text("👥 انتخاب کاربر آنلاین:" + FOOTER, reply_markup=kb, parse_mode="HTML")
            return await call.answer("رفرش شد ✅")
        except Exception as e:
            return await call.answer("خطا: " + str(e)[:200], show_alert=True)

    if call.data == "mg:back_users":
        try:
            items, _ = await fetch_clientlist_with_times()
            kb = kb_manage_pick_user(items).as_markup()
            await call.message.edit_text("👥 انتخاب کاربر آنلاین:" + FOOTER, reply_markup=kb, parse_mode="HTML")
            return await call.answer("✅")
        except Exception as e:
            return await call.answer("خطا: " + str(e)[:200], show_alert=True)

    if call.data == "mg:bans":
        try:
            bans = await ts_banlist()
            if not bans:
                kb = kb_manage_main().as_markup()
                return await call.message.edit_text("📛 لیست بن خالی است." + FOOTER, parse_mode="HTML", reply_markup=kb)
            kb = kb_ban_list(bans).as_markup()
            await call.message.edit_text("📛 Ban List (برای مدیریت یکی را انتخاب کن):" + FOOTER, reply_markup=kb, parse_mode="HTML")
            return await call.answer("✅")
        except Exception as e:
            return await call.answer("خطا: " + str(e)[:200], show_alert=True)

    if call.data == "mg:broadcast":
        user_ids, chat_ids = await db_get_broadcast_targets()
        pending_broadcast[call.from_user.id] = True
        await call.answer()
        return await call.message.reply(
            f"📣 <b>ارسال همگانی</b>\n"
            f"• کاربران ذخیره‌شده: <b>{len(user_ids)}</b>\n"
            f"• گپ‌های فعال: <b>{len(chat_ids)}</b>\n\n"
            f"✍️ حالا پیام رو بفرست تا برای همه ارسال کنم."
            f"{FOOTER}",
            parse_mode="HTML"
        )

    if call.data.startswith("mg:user:"):
        clid = int(call.data.split(":")[-1])
        kb = kb_manage_actions(clid).as_markup()
        await call.message.edit_text("🛠 عملیات را انتخاب کن:" + FOOTER, reply_markup=kb, parse_mode="HTML")
        return await call.answer("✅")

    if call.data.startswith("mg:act:"):
        _, _, action, clid_s = call.data.split(":", 3)
        clid = int(clid_s)

        if action == "show_ip":
            try:
                info = await fetch_clientinfo(clid)
                nick = ts_unescape(info.get("client_nickname", "User"))
                ip = info.get("connection_client_ip") or info.get("client_ip") or "Unknown"
                await call.answer()
                return await call.message.reply(f"🌐 IP کاربر <b>{nick}</b>:\n<code>{ip}</code>{FOOTER}", parse_mode="HTML")
            except Exception as e:
                return await call.answer("خطا: " + str(e)[:200], show_alert=True)

        pending_manage_action[call.from_user.id] = (clid, action)
        await call.answer()

        if action in ("kick_server", "kick_channel", "ban_perm"):
            return await call.message.reply("✍️ دلیل را بفرست:" + FOOTER, parse_mode="HTML")

        if action == "ban_temp":
            return await call.message.reply("⏳ مدت (ثانیه) + دلیل را بفرست.\nمثال: <code>600 اسپم</code>" + FOOTER, parse_mode="HTML")

        if action in ("add_group", "remove_group"):
            await refresh_servergroup_cache()
            return await call.message.reply("🔧 SGID را بفرست (عدد).\nمثال: <code>6</code>" + FOOTER, parse_mode="HTML")

        return await call.message.reply("❌ عملیات نامعتبر." + FOOTER, parse_mode="HTML")

    await call.answer("نامعتبر", show_alert=True)


@dp.callback_query(F.data.startswith("bn:"))
async def cb_bans(call: CallbackQuery):
    if call.message.chat.type != "private":
        return await call.answer("فقط در پیوی.", show_alert=True)
    if not await tg_is_in_main_group(call.from_user.id):
        return await call.answer("عضو گروه نیستید.", show_alert=True)
    if not is_whitelisted_admin(call.from_user.id):
        return await call.answer("دسترسی ندارید.", show_alert=True)

    parts = call.data.split(":")
    cmd = parts[1]
    banid = int(parts[2])

    if cmd == "open":
        try:
            bans = await ts_banlist()
            item = next((x for x in bans if int(x.get("banid", "0") or "0") == banid), None)
            if not item:
                return await call.answer("بن پیدا نشد", show_alert=True)

            name = ts_unescape(item.get("name", "") or "—")
            ip = item.get("ip", "") or "—"
            uid = item.get("uid", "") or "—"
            reason = ts_unescape(item.get("reason", "") or "—")
            duration = item.get("duration", "") or "0"
            created = item.get("created", "") or "—"

            txt = (
                f"📛 <b>Ban Detail</b>\n"
                f"• banid: <code>{banid}</code>\n"
                f"• name: {name}\n"
                f"• ip: <code>{ip}</code>\n"
                f"• uid: <code>{uid}</code>\n"
                f"• duration: <code>{duration}</code>\n"
                f"• created: <code>{created}</code>\n"
                f"• reason: {reason}"
                f"{FOOTER}"
            )
            kb = kb_ban_actions(banid).as_markup()
            await call.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
            return await call.answer("✅")
        except Exception as e:
            return await call.answer("خطا: " + str(e)[:200], show_alert=True)

    if cmd == "unban":
        try:
            await ts_unban(banid)
            await call.answer("Unban شد ✅", show_alert=True)

            bans = await ts_banlist()
            if not bans:
                kb = kb_manage_main().as_markup()
                return await call.message.edit_text("📛 لیست بن خالی است." + FOOTER, parse_mode="HTML", reply_markup=kb)
            kb = kb_ban_list(bans).as_markup()
            await call.message.edit_text("📛 Ban List (برای مدیریت یکی را انتخاب کن):" + FOOTER, reply_markup=kb, parse_mode="HTML")
            return
        except Exception as e:
            return await call.answer("خطا: " + str(e)[:200], show_alert=True)

    if cmd == "reason":
        pending_ban_reason_edit[call.from_user.id] = banid
        await call.answer()
        return await call.message.reply("✍️ ریزن جدید را بفرست:" + FOOTER, parse_mode="HTML")

    await call.answer("نامعتبر", show_alert=True)


@dp.message()
async def handle_text(message: Message):
    uid = message.from_user.id
    txt = (message.text or "").strip()


    if not await tg_is_in_main_group(uid):
        if message.chat.type == "private" and txt.startswith(("/", "!", ".")):
            return await message.reply("❌ برای استفاده از ربات باید عضو گروه اصلی باشید." + FOOTER, parse_mode="HTML")
        return


    if uid in pending_broadcast:
        if message.chat.type != "private" or not is_whitelisted_admin(uid):
            pending_broadcast.pop(uid, None)
            return

        pending_broadcast.pop(uid, None)

        if not txt:
            return await message.reply("❌ متن پیام خالیه." + FOOTER, parse_mode="HTML")

        user_ids, chat_ids = await db_get_broadcast_targets()

        ok_users = 0
        ok_chats = 0
        fail = 0

        # send to users
        for tid in user_ids:
            try:
                await bot.send_message(tid, txt)
                ok_users += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.03)  


        for cid in chat_ids:
            try:
                await bot.send_message(cid, txt)
                ok_chats += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.03)

        return await message.reply(
            f"✅ ارسال انجام شد.\n"
            f"• موفق به کاربران: <b>{ok_users}</b>\n"
            f"• موفق به گپ‌ها: <b>{ok_chats}</b>\n"
            f"• خطا: <b>{fail}</b>"
            f"{FOOTER}",
            parse_mode="HTML"
        )


    if uid in pending_verify:
        if not is_whitelisted_admin(uid):
            pending_verify.pop(uid, None)
            return

        clid = pending_verify.pop(uid)
        if not txt:
            return await message.reply("❌ متن خالیه. یه متن برای Verify بفرست." + FOOTER, parse_mode="HTML")

        try:
            await verify_add_og_and_description_by_text(clid, txt)
            return await message.reply(
                f"✅ Verify انجام شد.\n"
                f"رنک <code>{OG_GROUP_NAME}</code> داده شد و Description ست شد.\n"
                f"📝 <b>Description:</b> <code>Verify By NoxBot || {txt}</code>"
                f"{FOOTER}",
                parse_mode="HTML"
            )
        except Exception as e:
            return await message.reply("❌ Verify ناموفق:\n" + str(e) + FOOTER, parse_mode="HTML")


    if uid in pending_ban_reason_edit:
        if message.chat.type != "private" or not is_whitelisted_admin(uid):
            pending_ban_reason_edit.pop(uid, None)
            return

        banid = pending_ban_reason_edit.pop(uid)
        if not txt:
            return await message.reply("❌ ریزن خالیه." + FOOTER, parse_mode="HTML")
        try:
            await ts_banedit_reason(banid, txt)
            return await message.reply("✅ Reason آپدیت شد." + FOOTER, parse_mode="HTML")
        except Exception as e:
            return await message.reply("❌ ناموفق:\n" + str(e) + FOOTER, parse_mode="HTML")


    if uid in pending_manage_action:
        if message.chat.type != "private" or not is_whitelisted_admin(uid):
            pending_manage_action.pop(uid, None)
            return

        clid, action = pending_manage_action.pop(uid)

        try:
            if action == "kick_server":
                if not txt:
                    return await message.reply("❌ دلیل خالیه." + FOOTER, parse_mode="HTML")
                await kick_server(clid, message.from_user, txt)
                return await message.reply("✅ Kick Server انجام شد." + FOOTER, parse_mode="HTML")

            if action == "kick_channel":
                if not txt:
                    return await message.reply("❌ دلیل خالیه." + FOOTER, parse_mode="HTML")
                await kick_channel(clid, message.from_user, txt)
                return await message.reply("✅ Kick Channel انجام شد." + FOOTER, parse_mode="HTML")

            if action == "ban_perm":
                if not txt:
                    return await message.reply("❌ دلیل خالیه." + FOOTER, parse_mode="HTML")
                await ban_perm(clid, message.from_user, txt)
                return await message.reply("✅ Ban Permanent انجام شد." + FOOTER, parse_mode="HTML")

            if action == "ban_temp":
                m = re.match(r"^(\d+)\s*(.*)$", txt)
                if not m:
                    return await message.reply("❌ فرمت درست نیست. مثال: <code>600 اسپم</code>" + FOOTER, parse_mode="HTML")
                seconds = int(m.group(1))
                reason = (m.group(2) or "").strip() or "Temporary ban"
                await ban_temp(clid, message.from_user, seconds, reason)
                return await message.reply(f"✅ Ban Temp انجام شد ({seconds} ثانیه)." + FOOTER, parse_mode="HTML")

            if action == "add_group":
                if not txt.isdigit():
                    return await message.reply("❌ فقط SGID عددی بفرست." + FOOTER, parse_mode="HTML")
                sgid = int(txt)
                await add_server_group(clid, sgid)
                await refresh_servergroup_cache()
                name = servergroup_cache.get(sgid, str(sgid))
                return await message.reply(f"✅ Group اضافه شد: <b>{name}</b> (sgid={sgid}){FOOTER}", parse_mode="HTML")

            if action == "remove_group":
                if not txt.isdigit():
                    return await message.reply("❌ فقط SGID عددی بفرست." + FOOTER, parse_mode="HTML")
                sgid = int(txt)
                await remove_server_group(clid, sgid)
                await refresh_servergroup_cache()
                name = servergroup_cache.get(sgid, str(sgid))
                return await message.reply(f"✅ Group حذف شد: <b>{name}</b> (sgid={sgid}){FOOTER}", parse_mode="HTML")

            return await message.reply("❌ عملیات نامعتبر." + FOOTER, parse_mode="HTML")

        except Exception as e:
            return await message.reply("❌ عملیات ناموفق:\n" + str(e) + FOOTER, parse_mode="HTML")


    if uid in pending_poke:
        if message.chat.type in ("group", "supergroup") and message.chat.id != GROUP_ID:
            pending_poke.pop(uid, None)
            return
        clid = pending_poke.pop(uid)
        if not txt:
            return await message.reply("❌ متن پوک خالیه." + FOOTER, parse_mode="HTML")
        try:
            await poke(clid, message.from_user, txt)
            return await message.reply("✅ پوک ارسال شد." + FOOTER, parse_mode="HTML")
        except Exception as e:
            return await message.reply("❌ پوک ناموفق:\n" + str(e) + FOOTER, parse_mode="HTML")

    # --- pm flow ---
    if uid in pending_msg:
        if message.chat.type in ("group", "supergroup") and message.chat.id != GROUP_ID:
            pending_msg.pop(uid, None)
            return
        clid = pending_msg.pop(uid)
        if not txt:
            return await message.reply("❌ متن پیام خالیه." + FOOTER, parse_mode="HTML")
        try:
            await private_message(clid, message.from_user, txt)
            return await message.reply("✅ پیام خصوصی ارسال شد." + FOOTER, parse_mode="HTML")
        except Exception as e:
            return await message.reply("❌ پیام خصوصی ناموفق:\n" + str(e) + FOOTER, parse_mode="HTML")


async def ts_notify_loop():
    """
    Listen only:
      - notifycliententerview (connect)
    and log into LOG_CHAT_ID.
    """
    while True:
        try:
            reader, writer, _ = await ts_connect()

            r = await send_cmd(writer, reader, "servernotifyregister event=server")
            if not is_ok(r):
                raise RuntimeError(f"servernotifyregister failed:\n{r}")

            while True:
                line = await reader.readline()
                if not line:
                    raise RuntimeError("notify connection closed")

                text = line.decode("utf-8", errors="ignore").strip()

                if text.startswith("notifycliententerview"):
                    payload = text[len("notifycliententerview"):].strip()
                    data = {}
                    for part in payload.split():
                        if "=" in part:
                            k, v = part.split("=", 1)
                            data[k] = v

                    if data.get("client_type") == "1":
                        continue

                    nick = ts_unescape(data.get("client_nickname", "User"))
                    msg = (
                        f"🟢 <b>{nick}</b> به تیم‌اسپیک متصل شد.\n"
                        f"🌐 <b>TS IP:</b> <code>{TS_PUBLIC_ADDR}</code>"
                        f"{FOOTER}"
                    )
                    await bot.send_message(LOG_CHAT_ID, msg, parse_mode="HTML")

        except Exception:
            try:
                await asyncio.sleep(3)
            except Exception:
                pass


async def main():
    await db_init()
    asyncio.create_task(ts_notify_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
