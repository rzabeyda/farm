import asyncio, hashlib, hmac, json, os, sqlite3, time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           MenuButtonWebApp, Message, WebAppInfo)
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "freestars5bot")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://freestars.zabeyda.lol")
ADMIN_ID = int(os.environ.get("ADMIN_ID") or 0)
MIN_WITHDRAW = int(os.environ.get("MIN_WITHDRAW", 50))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "freestars.db")
BASE_REWARD = 1.0
REF_BONUS = 1.0  # +1 звезда рефереру каждый раз, когда его друг нажимает «Я не бот»

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
          balance REAL NOT NULL DEFAULT 0, last_claim TEXT, streak INTEGER NOT NULL DEFAULT 0,
          referrer_id INTEGER, ref_active INTEGER NOT NULL DEFAULT 0,
          created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS withdrawals(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created INTEGER NOT NULL);
        """)
        c.commit()


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ensure_user(c, uid, username, first_name, ref=None):
    if c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        c.execute("UPDATE users SET username=?, first_name=? WHERE id=?", (username, first_name, uid))
        return
    if ref == uid or not c.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
        ref = None
    c.execute("INSERT INTO users(id,username,first_name,referrer_id,created) VALUES(?,?,?,?,?)",
              (uid, username, first_name, ref, int(time.time())))


def parse_ref(param):
    if param and param.startswith("ref_") and param[4:].isdigit():
        return int(param[4:])
    return None


def verify_init_data(init_data: str) -> dict:
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            raise ValueError
        if time.time() - int(data.get("auth_date", 0)) > 86400:
            raise ValueError
        data["user"] = json.loads(data["user"])
        return data
    except Exception:
        raise HTTPException(401, "bad auth")


def rate(c, uid):
    n = c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=? AND ref_active=1", (uid,)).fetchone()[0]
    return n, BASE_REWARD


def state(c, uid):
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    n, daily = rate(c, uid)
    total = c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (uid,)).fetchone()[0]
    ftoday = c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=? AND last_claim=?", (uid, today())).fetchone()[0]
    fr = [{"name": (f["username"] and "@" + f["username"]) or f["first_name"] or f"id {f['id']}", "done": f["last_claim"] == today()}
          for f in c.execute("SELECT id,username,first_name,last_claim FROM users WHERE referrer_id=? ORDER BY (last_claim=?) DESC, created", (uid, today()))]
    yday = datetime.fromtimestamp(time.time() - 86400, timezone.utc).strftime("%Y-%m-%d")
    streak = u["streak"] if u["last_claim"] in (today(), yday) else 0
    pend = c.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE user_id=? AND status='pending'",
                     (uid,)).fetchone()[0]
    return {
        "balance": round(u["balance"], 4), "daily": daily, "max_daily": round(daily + REF_BONUS * n, 4), "refs": n, "refs_total": total,
        "friends_today": ftoday, "friends": fr,
        "claimed_today": u["last_claim"] == today(), "streak": streak,
        "min_withdraw": MIN_WITHDRAW, "pending": round(pend, 4),
        "link": f"https://t.me/{BOT_USERNAME}?startapp=ref_{uid}",
    }


@asynccontextmanager
async def lifespan(_):
    init_db()
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Звёзды", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception as e:
        print("menu button:", e)
    task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    task.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


def user_from(init_data):
    d = verify_init_data(init_data)
    return d["user"], parse_ref(d.get("start_param"))


@app.post("/api/me")
def api_me(x_init_data: str = Header()):
    u, ref = user_from(x_init_data)
    with closing(db()) as c:
        ensure_user(c, u["id"], u.get("username"), u.get("first_name"), ref)
        c.commit()
        return state(c, u["id"])


@app.post("/api/claim")
def api_claim(x_init_data: str = Header()):
    u, ref = user_from(x_init_data)
    with closing(db()) as c:
        ensure_user(c, u["id"], u.get("username"), u.get("first_name"), ref)
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        t = today()
        if row["last_claim"] == t:
            c.rollback()
            raise HTTPException(400, "already")
        _, daily = rate(c, u["id"])
        yesterday = datetime.fromtimestamp(time.time() - 86400, timezone.utc).strftime("%Y-%m-%d")
        streak = row["streak"] + 1 if row["last_claim"] == yesterday else 1
        c.execute("UPDATE users SET balance=balance+?, last_claim=?, streak=? WHERE id=?",
                  (daily, t, streak, u["id"]))
        if row["referrer_id"]:
            c.execute("UPDATE users SET ref_active=1 WHERE id=?", (u["id"],))
            c.execute("UPDATE users SET balance=balance+? WHERE id=?", (REF_BONUS, row["referrer_id"]))
        c.commit()
        s = state(c, u["id"])
        s["reward"] = daily
        return s


@app.post("/api/withdraw")
async def api_withdraw(x_init_data: str = Header()):
    u, _ = user_from(x_init_data)
    with closing(db()) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        if not row:
            c.rollback()
            raise HTTPException(404)
        amount = int(row["balance"])
        if amount < MIN_WITHDRAW:
            c.rollback()
            raise HTTPException(400, f"min {MIN_WITHDRAW}")
        c.execute("UPDATE users SET balance=balance-? WHERE id=?", (amount, u["id"]))
        cur = c.execute("INSERT INTO withdrawals(user_id,amount,created) VALUES(?,?,?)",
                        (u["id"], amount, int(time.time())))
        c.commit()
        s = state(c, u["id"])
        wid = cur.lastrowid
    if ADMIN_ID:
        try:
            name = f"@{u['username']}" if u.get("username") else u.get("first_name", "")
            await bot.send_message(ADMIN_ID, f"Вывод #{wid}: {amount} звёзд от {name} (id {u['id']})")
        except Exception:
            pass
    return s


@app.post("/api/history")
def api_history(x_init_data: str = Header()):
    u, _ = user_from(x_init_data)
    with closing(db()) as c:
        rows = c.execute("SELECT id,amount,status,created FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 50",
                         (u["id"],)).fetchall()
    return [{"id": r["id"], "amount": r["amount"], "status": r["status"], "created": r["created"]} for r in rows]


@dp.message(Command("paid"))
async def paid(m: Message, command: CommandObject):
    if not ADMIN_ID or m.from_user.id != ADMIN_ID:
        return
    if not (command.args or "").strip().isdigit():
        await m.answer("Использование: /paid НОМЕР_ЗАЯВКИ")
        return
    wid = int(command.args.strip())
    with closing(db()) as c:
        row = c.execute("SELECT user_id,amount FROM withdrawals WHERE id=? AND status='pending'", (wid,)).fetchone()
        if not row:
            await m.answer("Заявка не найдена или уже выплачена")
            return
        c.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (wid,))
        c.commit()
    await m.answer(f"Заявка #{wid} отмечена выплаченной")
    try:
        await bot.send_message(row["user_id"], f"Твой вывод #{wid} на {int(row['amount'])} звёзд выплачен")
    except Exception:
        pass

@dp.message(CommandStart())
async def start(m: Message, command: CommandObject):
    u = m.from_user
    with closing(db()) as c:
        ensure_user(c, u.id, u.username, u.first_name, parse_ref(command.args))
        c.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Привет! Заходи каждый день, жми «Я не бот» и получай Telegram Stars.\n"
                   "Приглашай друзей — +1 звезда в день за каждого, кто заходит и жмёт «Я не бот».", reply_markup=kb)


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"), headers={"Cache-Control": "no-store"})
