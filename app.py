import asyncio, glob, hashlib, hmac, json, os, shutil, sqlite3, time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
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
REMIND_HOUR = int(os.environ.get("REMIND_HOUR_UTC", 9))   # 12:00 по Москве
BACKUP_HOUR = int(os.environ.get("BACKUP_HOUR_UTC", 3))
BACKUP_DIR = os.path.join(HERE, "backups")
WELCOME_FILE_ID = None
BASE_REWARD = 1.0
# VIP-уровни за Telegram Stars: (цена в ⭐, бонус к награде в день, название)
STATUSES = {
    "gold": (25, 1, "Gold"),
    "platinum": (100, 5, "Platinum"),
    "diamond": (500, 25, "Diamond"),
}
REF_MIN_DAYS = 3  # друг начинает приносить бонус после стольких дней, в которые он нажал кнопку
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
        c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS activity(day TEXT NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY(day, user_id))")
        for ddl in ("ALTER TABLE users ADD COLUMN last_remind TEXT",
                    "ALTER TABLE users ADD COLUMN remind_off INTEGER NOT NULL DEFAULT 0"):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE users ADD COLUMN claim_days INTEGER NOT NULL DEFAULT 0")
            c.execute("UPDATE users SET claim_days=MAX(streak,1) WHERE last_claim IS NOT NULL")
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
        return False
    if ref == uid or not c.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
        ref = None
    c.execute("INSERT INTO users(id,username,first_name,referrer_id,created) VALUES(?,?,?,?,?)",
              (uid, username, first_name, ref, int(time.time())))
    return True


def touch(c, uid):
    c.execute("INSERT OR IGNORE INTO activity(day,user_id) VALUES(?,?)", (today(), uid))


async def notify_new(u, tag=None):
    if not ADMIN_ID or u["id"] == ADMIN_ID:
        return
    who = f"@{u['username']}" if u.get("username") else f"{u.get('first_name') or ''} (нет username)"
    text = f"Новый юзер {who}\nid {u['id']}"
    with closing(db()) as c:
        r = c.execute("SELECT r.id,r.username,r.first_name FROM users u JOIN users r ON r.id=u.referrer_id WHERE u.id=?",
                      (u["id"],)).fetchone()
    if r:
        text += " · пригласил " + (f"@{r['username']}" if r["username"] else (r["first_name"] or f"id {r['id']}"))
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as e:
        print("notify new:", e)


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
    ftoday = c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=? AND ref_active=1 AND last_claim=?", (uid, today())).fetchone()[0]
    fr = [{"name": (f["username"] and "@" + f["username"]) or f["first_name"] or f"id {f['id']}", "done": f["last_claim"] == today(), "days": f["claim_days"]}
          for f in c.execute("SELECT id,username,first_name,last_claim,claim_days FROM users WHERE referrer_id=? ORDER BY (last_claim=?) DESC, created", (uid, today()))]
    yday = datetime.fromtimestamp(time.time() - 86400, timezone.utc).strftime("%Y-%m-%d")
    streak = u["streak"] if u["last_claim"] in (today(), yday) else 0
    pend = c.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE user_id=? AND status='pending'",
                     (uid,)).fetchone()[0]
    return {
        "balance": round(u["balance"], 4), "daily": daily, "max_daily": round(daily + REF_BONUS * n, 4), "refs": n, "refs_total": total,
        "friends_today": ftoday, "friends": fr,
        "claimed_today": u["last_claim"] == today(), "streak": streak, "today": today(),
        "min_withdraw": MIN_WITHDRAW, "pending": round(pend, 4), "ref_min_days": REF_MIN_DAYS,
        "status": status_of(c, uid), "status_bonus": status_bonus(c, uid),
        "statuses": [{"key": k, "name": v[2], "price": v[0], "bonus": v[1], "pay": status_price(c, uid, k)}
                     for k, v in STATUSES.items()],
        "link": f"https://t.me/{BOT_USERNAME}?startapp=ref_{uid}",
    }


def make_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"freestars-{today()}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    for old in sorted(glob.glob(os.path.join(BACKUP_DIR, "freestars-*.db")))[:-14]:
        os.remove(old)
    return path


async def send_backup():
    path = await asyncio.to_thread(make_backup)
    if ADMIN_ID:
        try:
            await bot.send_document(ADMIN_ID, FSInputFile(path), caption=f"Бэкап базы {today()}")
        except Exception as e:
            print("backup send:", e)
    return path


def meta_get(k):
    with closing(db()) as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None


def meta_set(k, v):
    with closing(db()) as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        c.commit()


async def run_reminders():
    t = today()
    week_ago = datetime.fromtimestamp(time.time() - 7 * 86400, timezone.utc).strftime("%Y-%m-%d")
    with closing(db()) as c:
        users = c.execute(
            "SELECT id,streak,last_claim FROM users WHERE remind_off=0 AND (last_claim IS NULL OR last_claim<>?) "
            "AND (last_remind IS NULL OR last_remind<>?) "
            "AND ((last_claim IS NULL AND created>=?) OR last_claim>=?)",
            (t, t, int(time.time()) - 3 * 86400, week_ago)).fetchall()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Забрать звезду", web_app=WebAppInfo(url=WEBAPP_URL))]])
    sent = blocked = 0
    for u in users:
        text = "Твоя звезда за сегодня ждёт. Нажми «Я не бот» и забери её."
        if u["streak"] and u["last_claim"]:
            text += f"\nСерия {u['streak']} дн. — не потеряй её."
        text += "\nОтключить напоминания: /stop"
        try:
            await bot.send_message(u["id"], text, reply_markup=kb)
            sent += 1
            upd = "UPDATE users SET last_remind=? WHERE id=?"
            args = (t, u["id"])
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            continue
        except TelegramForbiddenError:
            blocked += 1
            upd = "UPDATE users SET remind_off=1, last_remind=? WHERE id=?"
            args = (t, u["id"])
        except Exception:
            continue
        with closing(db()) as c:
            c.execute(upd, args)
            c.commit()
        await asyncio.sleep(0.05)
    return sent, blocked, len(users)


async def scheduler():
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour >= BACKUP_HOUR and meta_get("backup_day") != today():
                meta_set("backup_day", today())
                await send_backup()
            if now.hour >= REMIND_HOUR and meta_get("remind_day") != today():
                meta_set("remind_day", today())
                sent, blocked, total = await run_reminders()
                if ADMIN_ID:
                    await bot.send_message(ADMIN_ID, f"Напоминания: отправлено {sent}, заблокировали бота {blocked}, из {total}")
        except Exception as e:
            print("scheduler:", e)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_):
    init_db()
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Звёзды", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception as e:
        print("menu button:", e)
    task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    sched = asyncio.create_task(scheduler())
    yield
    task.cancel()
    sched.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


def user_from(init_data):
    d = verify_init_data(init_data)
    return d["user"], parse_ref(d.get("start_param"))


@app.post("/api/me")
async def api_me(x_init_data: str = Header()):
    u, ref = user_from(x_init_data)
    with closing(db()) as c:
        new = ensure_user(c, u["id"], u.get("username"), u.get("first_name"), ref)
        touch(c, u["id"])
        c.commit()
        s = state(c, u["id"])
    if new:
        await notify_new(u)
    return s


@app.post("/api/claim")
async def api_claim(x_init_data: str = Header()):
    u, ref = user_from(x_init_data)
    with closing(db()) as c:
        new = ensure_user(c, u["id"], u.get("username"), u.get("first_name"), ref)
        touch(c, u["id"])
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
        days = row["claim_days"] + 1
        c.execute("UPDATE users SET claim_days=? WHERE id=?", (days, u["id"]))
        if row["referrer_id"] and days >= REF_MIN_DAYS:
            c.execute("UPDATE users SET ref_active=1 WHERE id=?", (u["id"],))
            c.execute("UPDATE users SET balance=balance+?, earned=earned+? WHERE id=?", (REF_BONUS, REF_BONUS, row["referrer_id"]))
        c.commit()
        s = state(c, u["id"])
        s["reward"] = daily
    if new:
        await notify_new(u)
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
            who = f"@{u['username']}" if u.get("username") else f"{u.get('first_name', '')} (нет username)"
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=f"Выплачено #{wid}", callback_data=f"pay:{wid}")]])
            await bot.send_message(ADMIN_ID, f"{who} запрос {amount} Stars\n#{wid} · id {u['id']}", reply_markup=kb)
        except Exception as e:
            print("withdraw notify:", e)
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
        title=f"VIP {name}", description=f"+{STATUSES[key][1]} звезд к доходу каждый день, навсегда",
        payload=f"status:{key}:{u['id']}", currency="XTR", prices=[LabeledPrice(label=f"VIP {name}", amount=pay)])
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
    await q.answer(ok=ok, error_message=None if ok else "Этот VIP-уровень уже недоступен для покупки")


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
    await m.answer(f"VIP {STATUSES[key][2]} активирован: +{STATUSES[key][1]} звезд к доходу каждый день")
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"Покупка VIP {STATUSES[key][2]}: {sp.total_amount} звёзд от id {uid}")
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


def admin_only(uid):
    return bool(ADMIN_ID) and uid == ADMIN_ID


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data="adm:stats"),
         InlineKeyboardButton(text="Заявки на вывод", callback_data="adm:wd")],
        [InlineKeyboardButton(text="Топ рефоводов", callback_data="adm:top")],
        [InlineKeyboardButton(text="Разослать напоминания", callback_data="adm:remind"),
         InlineKeyboardButton(text="Бэкап сейчас", callback_data="adm:backup")]])


def stats_text():
    t0 = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with closing(db()) as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        total = q("SELECT COUNT(*) FROM users")
        new_today = q("SELECT COUNT(*) FROM users WHERE created>=?", t0)
        new_yday = q("SELECT COUNT(*) FROM users WHERE created>=? AND created<?", t0 - 86400, t0)
        new_week = q("SELECT COUNT(*) FROM users WHERE created>=?", t0 - 6 * 86400)
        yday = datetime.fromtimestamp(time.time() - 86400, timezone.utc).strftime("%Y-%m-%d")
        seen_today = q("SELECT COUNT(*) FROM activity WHERE day=?", today())
        seen_yday = q("SELECT COUNT(*) FROM activity WHERE day=?", yday)
        active = q("SELECT COUNT(*) FROM users WHERE last_claim=?", today())
        with_refs = q("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL")
        owed = q("SELECT COALESCE(SUM(balance),0) FROM users")
        pend_n, pend_s = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals WHERE status='pending'").fetchone()
        paid_n, paid_s = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'").fetchone()
        buys_n, buys_s = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments").fetchone()
        st = {k: q("SELECT COUNT(*) FROM users WHERE status=?", k) for k in STATUSES}
    return (f"Статистика Free Stars\n\n"
            f"Всего вошло в бота: {total}\n"
            f"Новых: сегодня {new_today}, вчера {new_yday}, за 7 дней {new_week}\n"
            f"Заходили в приложение: сегодня {seen_today}, вчера {seen_yday}\n"
            f"Нажали кнопку сегодня: {active}\n"
            f"Пришли по приглашению: {with_refs}\n\n"
            f"Должны пользователям (балансы): {owed:.1f}\n"
            f"Заявок на вывод: {pend_n} на {pend_s:.0f}\n"
            f"Выплачено: {paid_n} на {paid_s:.0f}\n\n"
            f"Куплено VIP: {buys_n} на {buys_s} звёзд\n"
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


@dp.callback_query(F.data.startswith("pay:"))
async def pay_cb(q: CallbackQuery):
    if not admin_only(q.from_user.id):
        await q.answer()
        return
    wid = int(q.data.split(":")[1])
    row = await mark_paid(wid)
    await show(q, (q.message.text or "") + ("\n\nВыплачено" if row else "\n\nУже была выплачена"), None)
    try:
        await q.answer("Выплачено" if row else "Уже выплачена")
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
    elif parts[1] == "remind":
        sent, blocked, total = await run_reminders()
        note = f"Отправлено {sent}, заблокировали {blocked}, всего {total}"
    elif parts[1] == "backup":
        await send_backup()
        note = "Бэкап отправлен в чат"
    elif parts[1] == "wd":
        if len(parts) > 2 and parts[2] == "paid":
            row = await mark_paid(int(parts[3]))
            note = "Выплачено" if row else "Уже выплачена"
        with closing(db()) as c:
            rows = c.execute("SELECT w.id,w.amount,u.id uid,u.username,u.first_name FROM withdrawals w "
                             "JOIN users u ON u.id=w.user_id WHERE w.status='pending' ORDER BY w.id LIMIT 10").fetchall()
        if not rows:
            await show(q, "Заявок на вывод нет", admin_kb())
            try:
                await q.answer(note)
            except Exception:
                pass
            return
        text = "Заявки на вывод (нажми, чтобы отметить выплаченной):\n\n" + "\n".join(
            f"#{r['id']}: {int(r['amount'])} звёзд, {_name(r)}" + (f" (@{r['username']})" if r["username"] else " (нет username)")
            + f", id {r['uid']}" for r in rows)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"Выплачено #{r['id']} ({int(r['amount'])})", callback_data=f"adm:wd:paid:{r['id']}")] for r in rows]
            + [[InlineKeyboardButton(text="Назад", callback_data="adm:stats")]])
        await show(q, text, kb)
    try:
        await q.answer(note)
    except Exception:
        pass


@dp.message(Command("stop"))
async def stop_remind(m: Message):
    with closing(db()) as c:
        c.execute("UPDATE users SET remind_off=1 WHERE id=?", (m.from_user.id,))
        c.commit()
    await m.answer("Напоминания отключены. Включить снова: /remind")


@dp.message(Command("remind"))
async def start_remind(m: Message):
    with closing(db()) as c:
        c.execute("UPDATE users SET remind_off=0 WHERE id=?", (m.from_user.id,))
        c.commit()
    await m.answer("Напоминания включены")


@dp.message(CommandStart())
async def start(m: Message, command: CommandObject):
    u = m.from_user
    with closing(db()) as c:
        new = ensure_user(c, u.id, u.username, u.first_name, parse_ref(command.args))
        c.execute("UPDATE users SET remind_off=0 WHERE id=?", (u.id,))
        c.commit()
    if new:
        await notify_new({"id": u.id, "username": u.username, "first_name": u.first_name})
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
