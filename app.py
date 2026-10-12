import asyncio, hashlib, hmac, json, os, sqlite3, time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           CallbackQuery, LabeledPrice, MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo)
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
WELCOME_FILE_ID = None
BASE_REWARD = 1.0
# Статусы за Telegram Stars: (цена в ⭐, бонус к награде в день, название)
STATUSES = {
    "gold": (25, 1, "Gold"),
    "platinum": (100, 5, "Platinum"),
    "diamond": (500, 25, "Diamond"),
}
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
        CREATE TABLE IF NOT EXISTS payments(
          charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL,
          amount INTEGER NOT NULL, created INTEGER NOT NULL);
        """)
        try:
            c.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE users ADD COLUMN earned REAL NOT NULL DEFAULT 0")
            c.execute("UPDATE users SET earned=balance")
        except sqlite3.OperationalError:
            pass
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
    return n, BASE_REWARD + status_bonus(c, uid)


def status_of(c, uid):
    r = c.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
    return r["status"] if r and r["status"] in STATUSES else ""


def status_bonus(c, uid):
    s = status_of(c, uid)
    return STATUSES[s][1] if s else 0


def status_price(c, uid, key):
    cur = status_of(c, uid)
    return STATUSES[key][0] - (STATUSES[cur][0] if cur else 0)


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
        "status": status_of(c, uid), "status_bonus": status_bonus(c, uid),
        "statuses": [{"key": k, "name": v[2], "price": v[0], "bonus": v[1], "pay": status_price(c, uid, k)}
                     for k, v in STATUSES.items()],
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
        c.execute("UPDATE users SET balance=balance+?, earned=earned+?, last_claim=?, streak=? WHERE id=?",
                  (daily, daily, t, streak, u["id"]))
        if row["referrer_id"]:
            c.execute("UPDATE users SET ref_active=1 WHERE id=?", (u["id"],))
            c.execute("UPDATE users SET balance=balance+?, earned=earned+? WHERE id=?", (REF_BONUS, REF_BONUS, row["referrer_id"]))
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


@app.post("/api/buy/{key}")
async def api_buy(key: str, x_init_data: str = Header()):
    u, _ = user_from(x_init_data)
    if key not in STATUSES:
        raise HTTPException(404)
    with closing(db()) as c:
        ensure_user(c, u["id"], u.get("username"), u.get("first_name"))
        c.commit()
        cur = status_of(c, u["id"])
        if cur and STATUSES[cur][0] >= STATUSES[key][0]:
            raise HTTPException(400, "have")
        pay = status_price(c, u["id"], key)
    name = STATUSES[key][2]
    link = await bot.create_invoice_link(
        title=f"Статус {name}", description=f"+{STATUSES[key][1]} звезд к доходу каждый день, навсегда",
        payload=f"status:{key}:{u['id']}", currency="XTR", prices=[LabeledPrice(label=f"Статус {name}", amount=pay)])
    return {"link": link}


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = False
    try:
        _, key, uid = q.invoice_payload.split(":")
        with closing(db()) as c:
            cur = status_of(c, int(uid))
            ok = key in STATUSES and int(uid) == q.from_user.id and not (cur and STATUSES[cur][0] >= STATUSES[key][0])
    except Exception:
        pass
    await q.answer(ok=ok, error_message=None if ok else "Этот статус уже недоступен для покупки")


@dp.message(F.successful_payment)
async def paid_status(m: Message):
    sp = m.successful_payment
    _, key, uid = sp.invoice_payload.split(":")
    uid = int(uid)
    with closing(db()) as c:
        if c.execute("SELECT 1 FROM payments WHERE charge_id=?", (sp.telegram_payment_charge_id,)).fetchone():
            return
        c.execute("INSERT INTO payments(charge_id,user_id,status,amount,created) VALUES(?,?,?,?,?)",
                  (sp.telegram_payment_charge_id, uid, key, sp.total_amount, int(time.time())))
        c.execute("UPDATE users SET status=? WHERE id=?", (key, uid))
        c.commit()
    await m.answer(f"Статус {STATUSES[key][2]} активирован: +{STATUSES[key][1]} звезд к доходу каждый день")
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"Покупка статуса {STATUSES[key][2]}: {sp.total_amount} звёзд от id {uid}")
        except Exception:
            pass


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

def _name(r):
    return r["first_name"] or (r["username"] and "@" + r["username"]) or f"id {r['id']}"


@app.post("/api/top")
def api_top(x_init_data: str = Header()):
    u, _ = user_from(x_init_data)
    with closing(db()) as c:
        stars = [{"name": _name(r), "value": round(r["earned"], 2), "me": r["id"] == u["id"]}
                 for r in c.execute("SELECT id,username,first_name,earned FROM users WHERE earned>0 "
                                    "ORDER BY earned DESC, id LIMIT 20")]
        friends = [{"name": _name(r), "value": r["n"], "me": r["id"] == u["id"]}
                   for r in c.execute(
                       "SELECT u.id,u.username,u.first_name,COUNT(*) n FROM users f JOIN users u ON u.id=f.referrer_id "
                       "WHERE f.ref_active=1 GROUP BY u.id ORDER BY n DESC, u.id LIMIT 20")]
        me = c.execute("SELECT earned FROM users WHERE id=?", (u["id"],)).fetchone()
        my_rank = None
        if me and me["earned"] > 0:
            my_rank = c.execute("SELECT COUNT(*)+1 FROM users WHERE earned>?", (me["earned"],)).fetchone()[0]
    return {"stars": stars, "friends": friends, "my_rank": my_rank}


def admin_only(uid):
    return bool(ADMIN_ID) and uid == ADMIN_ID


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data="adm:stats"),
         InlineKeyboardButton(text="Заявки на вывод", callback_data="adm:wd")],
        [InlineKeyboardButton(text="Топ рефоводов", callback_data="adm:top")]])


def stats_text():
    t0 = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with closing(db()) as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        total = q("SELECT COUNT(*) FROM users")
        new_today = q("SELECT COUNT(*) FROM users WHERE created>=?", t0)
        new_week = q("SELECT COUNT(*) FROM users WHERE created>=?", t0 - 6 * 86400)
        active = q("SELECT COUNT(*) FROM users WHERE last_claim=?", today())
        with_refs = q("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL")
        owed = q("SELECT COALESCE(SUM(balance),0) FROM users")
        pend_n, pend_s = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals WHERE status='pending'").fetchone()
        paid_n, paid_s = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'").fetchone()
        buys_n, buys_s = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments").fetchone()
        st = {k: q("SELECT COUNT(*) FROM users WHERE status=?", k) for k in STATUSES}
    return (f"Статистика Free Stars\n\n"
            f"Пользователи: {total}\n"
            f"Новых сегодня: {new_today}, за 7 дней: {new_week}\n"
            f"Нажали сегодня: {active}\n"
            f"Пришли по приглашению: {with_refs}\n\n"
            f"Должны пользователям (балансы): {owed:.1f}\n"
            f"Заявок на вывод: {pend_n} на {pend_s:.0f}\n"
            f"Выплачено: {paid_n} на {paid_s:.0f}\n\n"
            f"Куплено статусов: {buys_n} на {buys_s} звёзд\n"
            + "  ".join(f"{STATUSES[k][2]}: {n}" for k, n in st.items()))


@dp.message(Command("admin"))
async def admin(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(stats_text(), reply_markup=admin_kb())


async def mark_paid(wid):
    with closing(db()) as c:
        row = c.execute("SELECT user_id,amount FROM withdrawals WHERE id=? AND status='pending'", (wid,)).fetchone()
        if not row:
            return None
        c.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (wid,))
        c.commit()
    try:
        await bot.send_message(row["user_id"], f"Твой вывод #{wid} на {int(row['amount'])} звёзд выплачен")
    except Exception:
        pass
    return row


async def show(q, text, kb):
    try:
        await q.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("adm:"))
async def admin_cb(q: CallbackQuery):
    if not admin_only(q.from_user.id):
        await q.answer()
        return
    parts = q.data.split(":")
    note = None
    if parts[1] == "stats":
        await show(q, stats_text(), admin_kb())
    elif parts[1] == "top":
        with closing(db()) as c:
            rows = c.execute("SELECT u.id,u.username,u.first_name,COUNT(*) n FROM users f JOIN users u ON u.id=f.referrer_id "
                             "WHERE f.ref_active=1 GROUP BY u.id ORDER BY n DESC LIMIT 15").fetchall()
        text = "Топ по друзьям (активные):\n\n" + ("\n".join(f"{i}. {_name(r)} (id {r['id']}): {r['n']}" for i, r in enumerate(rows, 1)) or "Пока пусто")
        await show(q, text, admin_kb())
    elif parts[1] == "wd":
        if len(parts) > 2 and parts[2] == "paid":
            row = await mark_paid(int(parts[3]))
            note = "Выплачено" if row else "Уже выплачена"
        with closing(db()) as c:
            rows = c.execute("SELECT w.id,w.amount,u.id uid,u.username,u.first_name FROM withdrawals w "
                             "JOIN users u ON u.id=w.user_id WHERE w.status='pending' ORDER BY w.id LIMIT 10").fetchall()
        if not rows:
            await show(q, "Заявок на вывод нет", admin_kb())
            await q.answer(note)
            return
        text = "Заявки на вывод (нажми, чтобы отметить выплаченной):\n\n" + "\n".join(
            f"#{r['id']}: {int(r['amount'])} звёзд, {_name(r)}" + (f" (@{r['username']})" if r["username"] else " (нет username)")
            + f", id {r['uid']}" for r in rows)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"Выплачено #{r['id']} ({int(r['amount'])})", callback_data=f"adm:wd:paid:{r['id']}")] for r in rows]
            + [[InlineKeyboardButton(text="Назад", callback_data="adm:stats")]])
        await show(q, text, kb)
    await q.answer(note)


@dp.message(CommandStart())
async def start(m: Message, command: CommandObject):
    u = m.from_user
    with closing(db()) as c:
        ensure_user(c, u.id, u.username, u.first_name, parse_ref(command.args))
        c.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL))]])
    text = ("Привет! Заходи каждый день, жми «Я не бот» и получай Telegram Stars.\n"
            "Приглашай друзей — +1 звезда в день за каждого, кто заходит и жмёт «Я не бот».")
    global WELCOME_FILE_ID
    try:
        sent = await m.answer_photo(WELCOME_FILE_ID or FSInputFile(os.path.join(HERE, "static", "main.jpg")),
                                    caption=text, reply_markup=kb)
        WELCOME_FILE_ID = sent.photo[-1].file_id
    except Exception as e:
        print("welcome photo:", e)
        await m.answer(text, reply_markup=kb)


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"), headers={"Cache-Control": "no-store"})
