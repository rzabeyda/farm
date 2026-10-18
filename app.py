import asyncio, glob, hashlib, hmac, json, os, sqlite3, time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                           MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo)
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import farm
from farm import UPGRADES

# Питомцы за Telegram Stars: ключ -> (цена в Stars, зарезервировано, название)
STATUSES = {k: (farm.TIERS[k]["stars"], 0, farm.TIERS[k]["name"]) for k in farm.PAID}

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "freestars5bot")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://freestars.zabeyda.lol")
ADMIN_ID = int(os.environ.get("ADMIN_ID") or 0)
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(HERE, "freestars.db")
REMIND_HOUR = int(os.environ.get("REMIND_HOUR_UTC", 9))   # 12:00 по Москве
BACKUP_HOUR = int(os.environ.get("BACKUP_HOUR_UTC", 3))
BACKUP_DIR = os.path.join(HERE, "backups")
WELCOME_FILE_ID = None
DEFAULT_PRIZE = "Лучшие игроки сезона получают призы в Telegram Stars из призового фонда. Размер фонда и условия объявляет администратор."

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
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS activity(day TEXT NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY(day, user_id));
        """)
        for ddl in ("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE users ADD COLUMN last_remind TEXT",
                    "ALTER TABLE users ADD COLUMN remind_off INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE users ADD COLUMN claim_days INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE users ADD COLUMN earned REAL NOT NULL DEFAULT 0",
                    "ALTER TABLE users ADD COLUMN season_points REAL NOT NULL DEFAULT 0",
                    "ALTER TABLE users ADD COLUMN energy REAL",
                    "ALTER TABLE users ADD COLUMN energy_ts REAL",
                    "ALTER TABLE users ADD COLUMN profit_ts REAL",
                    "ALTER TABLE users ADD COLUMN last_tap_ts REAL",
                    "ALTER TABLE users ADD COLUMN upg TEXT NOT NULL DEFAULT '{}'",
                    "ALTER TABLE users ADD COLUMN res REAL",
                    "ALTER TABLE users ADD COLUMN res_ts REAL"):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE users ADD COLUMN points REAL NOT NULL DEFAULT 0")
            # прежние накопленные звёзды превращаются в очки (1 звезда = 100 очков)
            c.execute("UPDATE users SET points=balance*100, season_points=balance*100")
        except sqlite3.OperationalError:
            pass
        c.commit()


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def yesterday():
    return datetime.fromtimestamp(time.time() - 86400, timezone.utc).strftime("%Y-%m-%d")


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


async def notify_new(u):
    if not ADMIN_ID or u["id"] == ADMIN_ID:
        return
    who = f"@{u['username']}" if u.get("username") else f"{u.get('first_name') or ''} (нет username)"
    try:
        await bot.send_message(ADMIN_ID, f"Новый юзер {who}")
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


def user_from(init_data):
    d = verify_init_data(init_data)
    return d["user"], parse_ref(d.get("start_param"))


def status_of(c, uid):
    r = c.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
    return r["status"] if r and r["status"] in STATUSES else ""


def status_price(c, uid, key):
    cur = status_of(c, uid)
    return STATUSES[key][0] - (STATUSES[cur][0] if cur else 0)


def _name(r):
    return r["first_name"] or (r["username"] and "@" + r["username"]) or f"id {r['id']}"


def state(c, uid, now=None):
    now = now or time.time()
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    st = farm.stats(u)
    t, y = today(), yesterday()
    res = min(st["cap"], (u["res"] or 0.0) + st["rate"] * max(0.0, now - (u["res_ts"] or now)) / 3600.0)
    streak = u["streak"] if u["last_claim"] in (t, y) else 0
    claimed = u["last_claim"] == t
    friends = [{"name": _name(f), "done": f["last_claim"] == t}
               for f in c.execute("SELECT id,username,first_name,last_claim FROM users WHERE referrer_id=? "
                                  "ORDER BY (last_claim=?) DESC, created", (uid, t))]
    refs_active = c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=? AND ref_active=1", (uid,)).fetchone()[0]
    ups = []
    for k, d in UPGRADES.items():
        lvl = st["levels"][k]
        ups.append({"key": k, "name": d["name"], "desc": d["desc"], "level": lvl, "max": d["max"],
                    "cost": farm.upgrade_cost(k, lvl) if lvl < d["max"] else None})
    tiers = []
    for k, v in farm.TIERS.items():
        tiers.append({"key": k, "name": v["name"], "tier": v["tier"], "stars": v["stars"], "res": v["res"],
                      "per_h": v["per_h"], "price": v["price"], "daily": v["daily"],
                      "pay": status_price(c, uid, k) if k in STATUSES else 0})
    return {
        "coins": round(u["points"], 2),
        "res": round(res, 2), "res_cap": round(st["cap"], 2), "res_name": st["t"]["res"],
        "rate": round(st["rate"], 2), "sell_price": round(st["price"], 2), "res_value": round(int(res) * st["price"], 2),
        "daily_reward": farm.farm_reward(streak + 1, st["t"]["daily"]),
        "cap_h": st["cap_h"],
        "animal": st["tier"], "animal_name": st["t"]["name"], "tier_name": st["t"]["tier"],
        "claimed_today": claimed, "streak": streak, "today": t,
        "friends": friends, "friends_today": sum(1 for f in friends if f["done"]), "refs": refs_active,
        "ref_bonus": farm.REF_BONUS_INVITER, "ref_bonus_friend": farm.REF_BONUS_FRIEND,
        "status": status_of(c, uid),
        "tiers": tiers,
        "upgrades": ups,
        "season_points": round(u["season_points"], 2),
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


def meta_get(k, default=None):
    with closing(db()) as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


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
        InlineKeyboardButton(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))]])
    sent = blocked = 0
    for u in users:
        text = "Твоя ферма ждёт. Зайди, нажми ФАРМ и забери награду."
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
            menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception as e:
        print("menu button:", e)
    task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    sched = asyncio.create_task(scheduler())
    yield
    task.cancel()
    sched.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.post("/api/me")
async def api_me(x_init_data: str = Header()):
    u, ref = user_from(x_init_data)
    with closing(db()) as c:
        new = ensure_user(c, u["id"], u.get("username"), u.get("first_name"), ref)
        touch(c, u["id"])
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        farm.settle(c, u["id"])
        c.commit()
        s = state(c, u["id"])
    if new:
        await notify_new(u)
    return s


@app.post("/api/claim")
async def api_claim(x_init_data: str = Header()):
    """Ежедневный ФАРМ: награда монетами, раз в сутки."""
    u, ref = user_from(x_init_data)
    with closing(db()) as c:
        new = ensure_user(c, u["id"], u.get("username"), u.get("first_name"), ref)
        touch(c, u["id"])
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        farm.settle(c, u["id"])
        row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        t = today()
        if row["last_claim"] == t:
            c.rollback()
            raise HTTPException(400, "already")
        streak = row["streak"] + 1 if row["last_claim"] == yesterday() else 1
        st = farm.stats(row)
        reward = farm.farm_reward(streak, st["t"]["daily"])
        first = row["claim_days"] == 0
        friend_bonus = farm.REF_BONUS_FRIEND if (first and row["referrer_id"]) else 0
        c.execute("UPDATE users SET points=points+?, season_points=season_points+?, last_claim=?, streak=?, "
                  "claim_days=claim_days+1 WHERE id=?",
                  (reward + friend_bonus, reward + friend_bonus, t, streak, u["id"]))
        if first and row["referrer_id"]:
            c.execute("UPDATE users SET ref_active=1 WHERE id=?", (u["id"],))
            c.execute("UPDATE users SET points=points+?, season_points=season_points+? WHERE id=?",
                      (farm.REF_BONUS_INVITER, farm.REF_BONUS_INVITER, row["referrer_id"]))
        c.commit()
        s = state(c, u["id"])
        s["reward"] = reward + friend_bonus
    if new:
        await notify_new(u)
    return s


@app.post("/api/sell")
def api_sell(x_init_data: str = Header()):
    """Продать ресурсы со склада за монеты по фиксированному курсу."""
    u, _ = user_from(x_init_data)
    with closing(db()) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        if not row:
            c.rollback()
            raise HTTPException(404)
        farm.settle(c, u["id"])
        row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        st = farm.stats(row)
        n = int(row["res"] or 0)
        gain = n * st["price"]
        if n > 0:
            c.execute("UPDATE users SET points=points+?, season_points=season_points+?, res=res-? WHERE id=?",
                      (gain, gain, n, u["id"]))
        c.commit()
        s = state(c, u["id"])
        s["sold"] = n
        s["gained"] = round(gain, 2)
        return s


@app.post("/api/upgrade/{key}")
def api_upgrade(key: str, x_init_data: str = Header()):
    if key not in UPGRADES:
        raise HTTPException(404)
    u, _ = user_from(x_init_data)
    with closing(db()) as c:
        c.execute("BEGIN IMMEDIATE")
        farm.settle(c, u["id"])
        row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        lv = farm.levels_of(row)
        if lv[key] >= UPGRADES[key]["max"]:
            c.rollback()
            raise HTTPException(400, "max")
        cost = farm.upgrade_cost(key, lv[key])
        if row["points"] < cost:
            c.rollback()
            raise HTTPException(400, "coins")
        lv[key] += 1
        c.execute("UPDATE users SET points=points-?, upg=? WHERE id=?", (cost, json.dumps(lv), u["id"]))
        c.commit()
        # ёмкость и скорость пересчитываются от нового уровня при ближайшем settle
        return state(c, u["id"])


@app.post("/api/rating")
def api_rating(x_init_data: str = Header()):
    u, _ = user_from(x_init_data)
    with closing(db()) as c:
        top = [{"name": _name(r), "value": round(r["season_points"]), "me": r["id"] == u["id"]}
               for r in c.execute("SELECT id,username,first_name,season_points FROM users WHERE season_points>0 "
                                  "ORDER BY season_points DESC, id LIMIT 20")]
        me = c.execute("SELECT season_points FROM users WHERE id=?", (u["id"],)).fetchone()
        rank = None
        if me and me["season_points"] > 0:
            rank = c.execute("SELECT COUNT(*)+1 FROM users WHERE season_points>?", (me["season_points"],)).fetchone()[0]
    return {"top": top, "rank": rank, "my": round(me["season_points"]) if me else 0,
            "season_start": meta_get("season_start") or "", "prize": meta_get("prize") or DEFAULT_PRIZE}


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
        title=f"Питомец: {name}", description=f"{name} поселится на твоей ферме навсегда и будет приносить ресурсы",
        payload=f"status:{key}:{u['id']}", currency="XTR", prices=[LabeledPrice(label=f"Питомец {name}", amount=pay)])
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
    await q.answer(ok=ok, error_message=None if ok else "Этот питомец уже недоступен для покупки")


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
    await m.answer(f"{STATUSES[key][2]} теперь живёт на твоей ферме")
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"Покупка питомца {STATUSES[key][2]}: {sp.total_amount} звёзд от id {uid}")
        except Exception:
            pass


# ---------------- админка ----------------

def admin_only(uid):
    return bool(ADMIN_ID) and uid == ADMIN_ID


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data="adm:stats"),
         InlineKeyboardButton(text="Рейтинг сезона", callback_data="adm:rating")],
        [InlineKeyboardButton(text="Топ рефоводов", callback_data="adm:top"),
         InlineKeyboardButton(text="Новый сезон", callback_data="adm:ns")],
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
        seen_today = q("SELECT COUNT(*) FROM activity WHERE day=?", today())
        seen_yday = q("SELECT COUNT(*) FROM activity WHERE day=?", yesterday())
        checked = q("SELECT COUNT(*) FROM users WHERE last_claim=?", today())
        with_refs = q("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL")
        pts = q("SELECT COALESCE(SUM(points),0) FROM users")
        buys_n, buys_s = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments").fetchone()
        st = {k: q("SELECT COUNT(*) FROM users WHERE status=?", k) for k in STATUSES}
    return (f"Статистика Free Stars\n\n"
            f"Всего вошло в бота: {total}\n"
            f"Новых: сегодня {new_today}, вчера {new_yday}, за 7 дней {new_week}\n"
            f"Заходили в приложение: сегодня {seen_today}, вчера {seen_yday}\n"
            f"Нажали ФАРМ сегодня: {checked}\n"
            f"Пришли по приглашению: {with_refs}\n\n"
            f"Монет у игроков всего: {pts:,.0f}\n\n"
            f"Куплено питомцев: {buys_n} на {buys_s} звёзд\n"
            + "  ".join(f"{STATUSES[k][2]}: {n}" for k, n in st.items()))


@dp.message(Command("admin"))
async def admin(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(stats_text(), reply_markup=admin_kb())


@dp.message(Command("prize"))
async def set_prize(m: Message, command: CommandObject):
    if not admin_only(m.from_user.id):
        return
    if not (command.args or "").strip():
        await m.answer("Использование: /prize ТЕКСТ (его увидят игроки в рейтинге). Сейчас:\n" + (meta_get("prize") or DEFAULT_PRIZE))
        return
    meta_set("prize", command.args.strip()[:500])
    await m.answer("Текст призов сохранён")


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
    elif parts[1] == "rating":
        with closing(db()) as c:
            rows = c.execute("SELECT id,username,first_name,season_points FROM users WHERE season_points>0 "
                             "ORDER BY season_points DESC LIMIT 15").fetchall()
        text = f"Рейтинг сезона (с {meta_get('season_start') or 'начала'}):\n\n" + (
            "\n".join(f"{i}. {_name(r)}" + (f" (@{r['username']})" if r["username"] else "") + f": {r['season_points']:,.0f}"
                      for i, r in enumerate(rows, 1)) or "Пока пусто")
        await show(q, text, admin_kb())
    elif parts[1] == "ns":
        if len(parts) > 2 and parts[2] == "yes":
            with closing(db()) as c:
                c.execute("UPDATE users SET season_points=0")
                c.commit()
            meta_set("season_start", today())
            note = "Новый сезон начат, очки рейтинга обнулены"
            await show(q, stats_text(), admin_kb())
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Да, начать новый сезон", callback_data="adm:ns:yes"),
                InlineKeyboardButton(text="Отмена", callback_data="adm:stats")]])
            await show(q, "Начать новый сезон? Очки рейтинга у всех обнулятся (монеты игроков останутся). "
                          "Сначала выплати призы за текущий сезон.", kb)
    elif parts[1] == "remind":
        sent, blocked, total = await run_reminders()
        note = f"Отправлено {sent}, заблокировали {blocked}, всего {total}"
    elif parts[1] == "backup":
        await send_backup()
        note = "Бэкап отправлен в чат"
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
        InlineKeyboardButton(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))]])
    text = ("Привет! Free Stars — ферма: нажимай ФАРМ раз в день, заводи питомцев, продавай ресурсы за монеты и улучшай ферму.\n"
            "Лучшие игроки сезона получают призы в Telegram Stars.")
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
