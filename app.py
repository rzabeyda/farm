import asyncio, glob, hashlib, hmac, html, json, os, random, re, sqlite3, time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, LinkPreviewOptions,
                           MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo)
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import farm

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
NFT_IDS = ["196787", "18775", "49993", "212312", "194519", "34351", "110883", "30846", "41869", "132494",
           "129754", "130195", "175066", "88462", "30452", "61032", "202183", "87790", "46133", "70214", "107748", "78965",
           "202928", "20370", "108354"]
# NFT других коллекций: "Коллекция-номер" как в ссылке t.me/nft/..., картинка static/nft/коллекция-номер.jpg
NFT_OTHER = ["LibertyFigure-261608", "LibertyFigure-67624", "SpringBasket-44493", "SnoopDogg-347559", "LibertyFigure-255648", "LibertyFigure-101787", "LibertyFigure-237012", "LibertyFigure-189626", "LibertyFigure-87941", "LibertyFigure-68725",
             "LibertyFigure-107834", "LibertyFigure-210306", "LibertyFigure-97753", "LibertyFigure-235419", "LibertyFigure-5044",
             "SnoopDogg-392692", "LibertyFigure-104039", "LibertyFigure-34553"]
NFT_PRICE = int(os.environ.get("NFT_PRICE", 243000))   # цена одного NFT в монетах по умолчанию
NFT_PRICE_TOP = 600000                                  # цена портфелей (NFT_TOP)
NFT_TOP = ["MoodPack-128844", "MoodPack-92875", "MoodPack-148916", "MoodPack-84020", "MoodPack-141418"]
NFT_PRICE_HIGH = 400000                                 # цена новых подарков (NFT_PREMIUM)
NFT_PREMIUM = set(NFT_OTHER) | {"PoolFloat-202928", "PoolFloat-20370", "PoolFloat-108354"}

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
        CREATE TABLE IF NOT EXISTS pending_grants(username TEXT PRIMARY KEY, pet TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ref_credit(day TEXT NOT NULL, inviter INTEGER NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, inviter));
        CREATE TABLE IF NOT EXISTS rewards(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, descr TEXT NOT NULL DEFAULT '',
          price INTEGER NOT NULL, stock INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS reward_orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, reward_id INTEGER NOT NULL,
          title TEXT NOT NULL, price INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created INTEGER NOT NULL);
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
                    "ALTER TABLE users ADD COLUMN res_ts REAL",
                    "ALTER TABLE users ADD COLUMN farm_until REAL",
                    "ALTER TABLE users ADD COLUMN pets TEXT NOT NULL DEFAULT '{}'"):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE users ADD COLUMN points REAL NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # один раз при переходе на ферму: накопленные звёзды превращаются в монеты (1 звезда = 100 монет)
        if not c.execute("SELECT 1 FROM meta WHERE k='farm_import'").fetchone():
            c.execute("UPDATE users SET points=balance*100, season_points=balance*100")
            c.execute("INSERT INTO meta(k,v) VALUES('farm_import','1')")
        for ddl in ("ALTER TABLE rewards ADD COLUMN img TEXT", "ALTER TABLE rewards ADD COLUMN url TEXT"):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # v3: нераспроданные ресурсы превращаются в монеты (ресурсы больше не копятся на складе)
        if not c.execute("SELECT 1 FROM meta WHERE k='farm3_import'").fetchone():
            for r in c.execute("SELECT id,status,res FROM users WHERE res IS NOT NULL AND res>=1").fetchall():
                k = r["status"] if r["status"] in farm.TIERS else "farmer"
                gain = int(r["res"]) * farm.TIERS[k]["price"]
                c.execute("UPDATE users SET points=points+?, season_points=season_points+?, res=0 WHERE id=?", (gain, gain, r["id"]))
            c.execute("INSERT INTO meta(k,v) VALUES('farm3_import','1')")
        # v4: ферма работает круглые сутки, пока нажимаешь ФАРМ; для тех, кто уже нажимал, включаем до конца следующих суток
        if not c.execute("SELECT 1 FROM meta WHERE k='farm4'").fetchone():
            for r in c.execute("SELECT id,last_claim FROM users WHERE last_claim IS NOT NULL").fetchall():
                d = datetime.strptime(r["last_claim"], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=2)
                c.execute("UPDATE users SET farm_until=? WHERE id=?", (d.timestamp(), r["id"]))
            c.execute("INSERT INTO meta(k,v) VALUES('farm4','1')")
        # v5: питомцев можно иметь много и разных видов (раньше был один статус)
        if not c.execute("SELECT 1 FROM meta WHERE k='pets5'").fetchone():
            for r in c.execute("SELECT id,status FROM users WHERE status<>'' AND status IS NOT NULL").fetchall():
                if r["status"] in farm.PAID:
                    c.execute("UPDATE users SET pets=? WHERE id=? AND pets='{}'", (json.dumps({r["status"]: 1}), r["id"]))
            c.execute("INSERT INTO meta(k,v) VALUES('pets5','1')")
        # однократно: вернуть в зачёт всех друзей, которые нажали ФАРМ (проверку скорости убрали, считаем по первому ФАРМ)
        if not c.execute("SELECT 1 FROM meta WHERE k='refrestore1'").fetchone():
            c.execute("UPDATE users SET ref_active=1 WHERE referrer_id IS NOT NULL AND claim_days>=1")
            c.execute("INSERT INTO meta(k,v) VALUES('refrestore1','1')")
        # NFT для обмена: каждая ссылка добавляется один раз. Повторы в списке, а также NFT, которые уже есть в базе
        # (проданные или снятые тоже), заново не добавляются.
        for name in dict.fromkeys([f"PoolFloat-{n}" for n in NFT_IDS] + NFT_OTHER + NFT_TOP):
            slug, nid = name.rsplit("-", 1)
            url = f"https://t.me/nft/{name}"
            if not c.execute("SELECT 1 FROM rewards WHERE url=?", (url,)).fetchone():
                title = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", slug) + f" #{nid}"
                img = f"static/nft/{nid}.jpg" if slug == "PoolFloat" else f"static/nft/{slug.lower()}-{nid}.jpg"
                c.execute("INSERT INTO rewards(title,descr,price,stock,created,img,url) VALUES(?,?,?,?,?,?,?)",
                          (title, "Коллекционный NFT, один экземпляр", NFT_PRICE_TOP if name in NFT_TOP else NFT_PRICE_HIGH if name in NFT_PREMIUM else NFT_PRICE, 1,
                           int(time.time()), img, url))
        c.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('nft_seed','1')")
        c.commit()


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def yesterday():
    return datetime.fromtimestamp(time.time() - 86400, timezone.utc).strftime("%Y-%m-%d")


def give_pet(c, uid, key):
    """Выдать питомца бесплатно (админская выдача). Доход до выдачи считается по старой ставке."""
    farm.settle(c, uid)
    row = c.execute("SELECT pets FROM users WHERE id=?", (uid,)).fetchone()
    try:
        d = json.loads(row["pets"] or "{}") if row else {}
    except Exception:
        d = {}
    d[key] = int(d.get(key, 0)) + 1
    c.execute("UPDATE users SET pets=? WHERE id=?", (json.dumps(d), uid))


def apply_grants(c, uid, username):
    """Если админ заранее выдал питомца по @username, а игрок пришёл только сейчас, выдаём при первом входе."""
    if not username:
        return
    g = c.execute("SELECT pet FROM pending_grants WHERE username=?", (username.lower(),)).fetchone()
    if g:
        c.execute("DELETE FROM pending_grants WHERE username=?", (username.lower(),))
        give_pet(c, uid, g["pet"])


def ensure_user(c, uid, username, first_name, ref=None):
    if c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        c.execute("UPDATE users SET username=?, first_name=? WHERE id=?", (username, first_name, uid))
        apply_grants(c, uid, username)
        return False
    if ref == uid or not c.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
        ref = None
    c.execute("INSERT INTO users(id,username,first_name,referrer_id,created) VALUES(?,?,?,?,?)",
              (uid, username, first_name, ref, int(time.time())))
    apply_grants(c, uid, username)
    return True


def touch(c, uid):
    c.execute("INSERT OR IGNORE INTO activity(day,user_id) VALUES(?,?)", (today(), uid))


async def notify_new(u):
    """Админу коротко: «Новый юзер @username», а если его позвал друг: «@друг привёл @username»."""
    if not ADMIN_ID or u["id"] == ADMIN_ID:
        return
    who = f"@{u['username']}" if u.get("username") else f"{u.get('first_name') or ''} (нет username)"
    with closing(db()) as c:
        r = c.execute("SELECT r.id,r.username,r.first_name FROM users n JOIN users r ON r.id=n.referrer_id WHERE n.id=?",
                      (u["id"],)).fetchone()
    if r:
        inviter = f"@{r['username']}" if r["username"] else (f"{r['first_name']} (нет username)" if r["first_name"] else f"id {r['id']}")
        text = f"{inviter} привёл {who}"
    else:
        text = f"Новый юзер {who}"
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


def user_from(init_data):
    d = verify_init_data(init_data)
    return d["user"], parse_ref(d.get("start_param"))


def _name(r):
    return r["first_name"] or (r["username"] and "@" + r["username"]) or f"id {r['id']}"


def state(c, uid, now=None):
    now = now or time.time()
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    st = farm.stats(u)
    t, y = today(), yesterday()
    streak = u["streak"] if u["last_claim"] in (t, y) else 0
    claimed = u["last_claim"] == t
    until = u["farm_until"] or 0
    friends = [{"name": _name(f), "username": f["username"], "done": f["last_claim"] == t, "counted": bool(f["ref_active"])}
               for f in c.execute("SELECT id,username,first_name,last_claim,ref_active FROM users WHERE referrer_id=? "
                                  "ORDER BY (last_claim=?) DESC, created", (uid, t))]
    refs_active = sum(1 for f in friends if f["done"])
    got = c.execute("SELECT n FROM ref_credit WHERE day=? AND inviter=?", (t, uid)).fetchone()
    ref_today = (got["n"] if got else 0) * farm.REF_BONUS
    tiers = []
    for k, v in farm.TIERS.items():
        tiers.append({"key": k, "name": v["name"], "short": v.get("short", v["name"]), "stars": v["stars"], "rate": farm.coin_rate(k),
                      "pay": STATUSES[k][0] if k in STATUSES else 0, "coin_price": farm.coin_price(k),
                      "owned": 1 if k == "farmer" else st["pets"].get(k, 0)})
    return {
        "coins": round(u["points"], 2),
        "rate": farm.rate_of(st["pets"]) + farm.streak_rate(streak), "streak_bonus": streak,
        "ref_bonus": farm.REF_BONUS, "ref_cap": farm.REF_CAP, "ref_today": ref_today,
        "farm_until": until, "farming": until > now,
        "is_admin": bool(ADMIN_ID) and uid == ADMIN_ID,
        "animal": st["tier"], "animal_name": st["t"]["name"],
        "claimed_today": claimed, "streak": streak, "today": t,
        "friends": friends, "friends_today": refs_active, "refs": refs_active,
        "refs_counted": sum(1 for f in friends if f["counted"]),     # друзья, нажавшие ФАРМ хотя бы раз: они же идут в гонку
        "pets": [{"key": k, "name": farm.TIERS[k]["name"], "count": 1 if k == "farmer" else st["pets"].get(k, 0)}
                 for k in farm.TIERS if k == "farmer" or st["pets"].get(k, 0) > 0],
        "tiers": tiers,
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
    """Копия базы раз в сутки: тихо сохраняется на сервере (последние 14 штук), в чат ничего не присылается."""
    return await asyncio.to_thread(make_backup)


def meta_get(k, default=None):
    with closing(db()) as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def meta_set(k, v):
    with closing(db()) as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        c.commit()


RACE_END = datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp()     # реферальная гонка: итоги 25.10.2026 15:00 МСК (12:00 UTC), до этого момента считаем


def race_start():
    """С какого момента считаем. По умолчанию с самого начала: кто уже приводил друзей, тот уже в зачёте.
    /refreset (админ) обнуляет счёт и ставит начало на этот момент."""
    v = meta_get("race_start")
    return int(v) if v else 0


PRIZES_URL = "https://t.me/peeppo_chat/32966"      # пост с призами в чате


def race_text(uid=None, as_html=False):
    """Топ 5 по приглашённым. Считаем друзей, которые пришли по ссылке и нажали ФАРМ (все, кто привёл раньше, тоже в зачёте)."""
    start = race_start()
    end = int(RACE_END)
    finished = time.time() >= end
    with closing(db()) as c:
        rows = c.execute("SELECT u.id,u.username,u.first_name,COUNT(*) n FROM users f JOIN users u ON u.id=f.referrer_id "
                         "WHERE f.ref_active=1 AND f.created>=? AND f.created<? AND u.id<>? GROUP BY u.id "
                         "ORDER BY n DESC, MIN(f.created) LIMIT 5", (start, end, ADMIN_ID or 0)).fetchall()
        mine = None
        if uid and uid != ADMIN_ID:
            mine = c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=? AND ref_active=1 AND created>=? AND created<?",
                             (uid, start, end)).fetchone()[0]
    lines = ['Реферальная гонка "Ферма"', "Итоги 25.10.2026 15:00 МСК" if not finished else "Конкурс завершён, итоги подведены",
             'Новый юзер нажми "ФАРМ"', ""]
    esc = html.escape if as_html else (lambda x: x)
    lines += [f"{i}. " + (f"@{r['username']}" if r["username"] else esc(_name(r))) + f" — {r['n']}" for i, r in enumerate(rows, 1)] or ["Пока никого. Приводи друзей!"]
    if mine is not None and not finished:
        lines += ["", f"Ты привёл: {mine}"]
    lines += ["", f'<a href="{PRIZES_URL}">NFT Призы</a>' if as_html else f"NFT Призы: {PRIZES_URL}"]
    return "\n".join(lines)


# напоминания в общий чат: за сколько до конца гонки и что писать. Каждое уходит один раз.
RACE_REMINDERS = [(72 * 3600, "3 дня"), (48 * 3600, "2 дня"), (24 * 3600, "1 день"), (12 * 3600, "12 часов"),
                  (4 * 3600, "4 часа"), (3600, "1 час"), (15 * 60, "15 минут")]


def race_due(now=None):
    """Какое напоминание пора отправить в чат: текст или None. Отметка «отправлено» ставится сразу, пропущенные
    более ранние напоминания отдельно не шлются (пишем только самое свежее)."""
    now = now if now is not None else time.time()
    if now >= RACE_END:
        return None
    due = [(secs, label) for secs, label in RACE_REMINDERS if now >= RACE_END - secs]
    fresh = [(secs, label) for secs, label in due if meta_get(f"race_sent:{secs}") is None]
    if not fresh:
        return None
    for secs, _ in due:
        meta_set(f"race_sent:{secs}", "1")
    label = min(fresh)[1]
    return f"До конца реферальной гонки осталось {label}" + chr(10) * 2 + race_text(as_html=True)


@dp.message(Command("ref"), F.chat.type == "private")      # в общем чате на /ref отвечает бот Peeppo (данные берёт у нас)
async def cmd_ref(m: Message):
    await m.answer(race_text(m.from_user.id if m.from_user else None, as_html=True), parse_mode="HTML",
                   link_preview_options=LinkPreviewOptions(is_disabled=True))


@dp.message(Command("refreset"))
async def cmd_refreset(m: Message):
    """Админ: обнулить гонку (счёт идёт заново с этого момента)."""
    if not admin_only(m.from_user.id):
        return
    meta_set("race_start", str(int(time.time())))
    await m.answer("Реферальная гонка обнулена, считаем заново")


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
        text = "Нажми ФАРМ, ферма ждёт."
        if u["streak"] and u["last_claim"]:
            text += f" Серия {u['streak']} дн."
        text += "\nОтключить: /stop"
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
                await run_reminders()
        except Exception as e:
            print("scheduler:", e)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_):
    init_db()
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL)))
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
    """ФАРМ раз в сутки: ферма работает круглые сутки до конца следующих суток (UTC)."""
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
        first = row["claim_days"] == 0
        c.execute("UPDATE users SET last_claim=?, streak=?, claim_days=claim_days+1, farm_until=?, res_ts=? WHERE id=?",
                  (t, streak, farm.farm_until_for(), time.time(), u["id"]))
        inv = row["referrer_id"]
        if inv:
            # друг нажал ФАРМ: он сразу засчитывается везде (гонка, галочка в профиле, монеты пригласившему)
            if first:
                c.execute("UPDATE users SET ref_active=1 WHERE id=?", (u["id"],))
            got = c.execute("SELECT n FROM ref_credit WHERE day=? AND inviter=?", (t, inv)).fetchone()
            if (got["n"] if got else 0) < farm.REF_CAP:
                # друг нажал ФАРМ: приглашающему +REF_BONUS (в первый раз сразу, затем в каждый день, когда друг играет)
                c.execute("UPDATE users SET points=points+?, season_points=season_points+? WHERE id=?",
                          (farm.REF_BONUS, farm.REF_BONUS, inv))
                c.execute("INSERT INTO ref_credit(day,inviter,n) VALUES(?,?,1) ON CONFLICT(day,inviter) DO UPDATE SET n=n+1", (t, inv))
        c.commit()
        s = state(c, u["id"])
        s["reward"] = 0
    if new:
        await notify_new(u)
    return s


@app.post("/api/shop")
def api_shop(x_init_data: str = Header()):
    """Магазин наград: коллекционные призы за монеты в ограниченном количестве."""
    u, _ = user_from(x_init_data)
    with closing(db()) as c:
        items = [{"id": r["id"], "title": r["title"], "descr": r["descr"], "price": r["price"], "stock": r["stock"],
                  "img": r["img"], "url": r["url"]}
                 for r in c.execute("SELECT id,title,descr,price,stock,img,url FROM rewards WHERE active=1 AND stock>0 ORDER BY (id * 2654435761) % 4294967296")]
        me = c.execute("SELECT points FROM users WHERE id=?", (u["id"],)).fetchone()
        mine = [{"title": r["title"], "status": r["status"]}
                for r in c.execute("SELECT title,status FROM reward_orders WHERE user_id=? ORDER BY id DESC LIMIT 20", (u["id"],))]
    return {"items": items, "coins": round(me["points"], 2) if me else 0, "mine": mine}


@app.post("/api/shop/buy/{rid}")
async def api_shop_buy(rid: int, x_init_data: str = Header()):
    u, _ = user_from(x_init_data)
    with closing(db()) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        rw = c.execute("SELECT * FROM rewards WHERE id=? AND active=1", (rid,)).fetchone()
        if not row or not rw:
            c.rollback()
            raise HTTPException(404)
        if not u.get("username"):
            c.rollback()
            raise HTTPException(400, "username")
        if rw["stock"] <= 0:
            c.rollback()
            raise HTTPException(400, "stock")
        if row["points"] < rw["price"]:
            c.rollback()
            raise HTTPException(400, "coins")
        c.execute("UPDATE users SET points=points-? WHERE id=?", (rw["price"], u["id"]))
        c.execute("UPDATE rewards SET stock=stock-1 WHERE id=?", (rid,))
        cur = c.execute("INSERT INTO reward_orders(user_id,reward_id,title,price,created) VALUES(?,?,?,?,?)",
                        (u["id"], rid, rw["title"], rw["price"], int(time.time())))
        oid = cur.lastrowid
        c.commit()
        s = state(c, u["id"])
    s["order"] = oid
    s["reward_title"] = rw["title"]
    if ADMIN_ID:
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"Выдано #{oid}", callback_data=f"ord:{oid}")]])
            await bot.send_message(ADMIN_ID, f"@{u['username']} заказал «{rw['title']}» за {rw['price']} монет\n#{oid}", reply_markup=kb)
        except Exception as e:
            print("order notify:", e)
    return s


@app.post("/api/shop/exchange")
async def api_shop_exchange(price: int = 0, x_init_data: str = Header()):
    """Обмен монет на случайный NFT выбранной цены (price). Выбирает сервер, игрок конкретный NFT выбрать не может."""
    u, _ = user_from(x_init_data)
    with closing(db()) as c:
        me = c.execute("SELECT points FROM users WHERE id=?", (u["id"],)).fetchone()
        coins = me["points"] if me else 0
        if price:
            rows = c.execute("SELECT id,price FROM rewards WHERE active=1 AND stock>0 AND price=?", (price,)).fetchall()
        else:
            rows = c.execute("SELECT id,price FROM rewards WHERE active=1 AND stock>0").fetchall()
    if not rows:
        raise HTTPException(400, "stock")
    ids = [r["id"] for r in rows if r["price"] <= coins]
    if not ids:
        raise HTTPException(400, "coins")
    return await api_shop_buy(random.choice(ids), x_init_data)


@app.get("/api/race")
def api_race():
    """Текст реферальной гонки без личных данных. Его забирает бот Peeppo: в общем чате /ref отвечает он, а данные берёт отсюда."""
    return Response(race_text(as_html=True), media_type="text/plain; charset=utf-8")


@app.post("/api/race/pop")
def api_race_pop(request: Request):
    """Только для бота Peeppo: забрать напоминание о гонке, если пора (его пост в общий чат). Снаружи (через nginx) закрыто."""
    if request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for"):
        raise HTTPException(403)
    return Response(race_due() or "", media_type="text/plain; charset=utf-8")


@app.post("/api/rating")
def api_rating(x_init_data: str = Header()):
    """Топ 10 игроков по заработанным монетам за всё время (потраченные тоже считаются)."""
    u, _ = user_from(x_init_data)
    with closing(db()) as c:
        top = [{"name": _name(r)[:15], "value": round(r["season_points"]), "me": r["id"] == u["id"], "done": r["last_claim"] == today()}
               for r in c.execute("SELECT id,username,first_name,season_points,last_claim FROM users WHERE season_points>0 "
                                  "AND id<>? ORDER BY season_points DESC, id LIMIT 10", (ADMIN_ID or 0,))]
        me = c.execute("SELECT season_points FROM users WHERE id=?", (u["id"],)).fetchone()
        rank = None
        if me and me["season_points"] > 0 and u["id"] != ADMIN_ID:     # админ в рейтинге не участвует
            rank = c.execute("SELECT COUNT(*)+1 FROM users WHERE season_points>? AND id<>?",
                             (me["season_points"], ADMIN_ID or 0)).fetchone()[0]
    return {"top": top, "rank": rank, "my": round(me["season_points"]) if me else 0}


@app.post("/api/buy_coins/{key}")
async def api_buy_coins(key: str, x_init_data: str = Header()):
    """Питомец за монеты (вместо Stars). Для админа бесплатно, как и покупка за Stars."""
    u, _ = user_from(x_init_data)
    if key not in STATUSES:
        raise HTTPException(404)
    with closing(db()) as c:
        ensure_user(c, u["id"], u.get("username"), u.get("first_name"))
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        farm.settle(c, u["id"])          # до смены питомцев доход считается по старой ставке
        row = c.execute("SELECT points FROM users WHERE id=?", (u["id"],)).fetchone()
        price = farm.coin_price(key)
        free = admin_only(u["id"])
        if not free and row["points"] < price:
            c.rollback()
            raise HTTPException(400, "coins")
        if not free:
            c.execute("UPDATE users SET points=points-? WHERE id=?", (price, u["id"]))
        give_pet(c, u["id"], key)
        c.commit()
        s = state(c, u["id"])
    s["bought_for_coins"] = key
    return s


@app.post("/api/buy/{key}")
async def api_buy(key: str, x_init_data: str = Header()):
    u, _ = user_from(x_init_data)
    if key not in STATUSES:
        raise HTTPException(404)
    with closing(db()) as c:
        ensure_user(c, u["id"], u.get("username"), u.get("first_name"))
        c.commit()
    if admin_only(u["id"]):
        # тест для админа: питомец выдаётся бесплатно, без счёта в Stars (платёж не создаётся и в выручку не попадает)
        with closing(db()) as c:
            c.execute("BEGIN IMMEDIATE")
            farm.settle(c, u["id"])          # до смены питомцев доход считается по старой ставке
            row = c.execute("SELECT pets FROM users WHERE id=?", (u["id"],)).fetchone()
            try:
                d = json.loads(row["pets"] or "{}") if row else {}
            except Exception:
                d = {}
            d[key] = int(d.get(key, 0)) + 1
            c.execute("UPDATE users SET pets=? WHERE id=?", (json.dumps(d), u["id"]))
            c.commit()
            s = state(c, u["id"])
        s["granted"] = key
        return s
    pay = STATUSES[key][0]          # питомцев можно покупать по несколько штук, цена всегда полная
    name = STATUSES[key][2]
    link = await bot.create_invoice_link(
        title=f"Питомец: {name}", description=f"{name} поселится на твоей ферме навсегда и будет приносить монеты. Питомцев можно иметь несколько",
        payload=f"status:{key}:{u['id']}", currency="XTR", prices=[LabeledPrice(label=f"Питомец {name}", amount=pay)])
    return {"link": link}


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = False
    try:
        _, key, uid = q.invoice_payload.split(":")
        ok = key in STATUSES and int(uid) == q.from_user.id
    except Exception:
        pass
    await q.answer(ok=ok, error_message=None if ok else "Не удалось оформить покупку питомца")


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
        farm.settle(c, uid)          # до смены питомцев доход считается по старой ставке
        row = c.execute("SELECT pets FROM users WHERE id=?", (uid,)).fetchone()
        try:
            d = json.loads(row["pets"] or "{}") if row else {}
        except Exception:
            d = {}
        d[key] = int(d.get(key, 0)) + 1
        c.execute("UPDATE users SET pets=? WHERE id=?", (json.dumps(d), uid))
        c.commit()
    await m.answer(f"{STATUSES[key][2]} теперь живёт на твоей ферме")
    if ADMIN_ID:
        try:
            with closing(db()) as c:
                u = c.execute("SELECT id,username,first_name FROM users WHERE id=?", (uid,)).fetchone()
            who = (f"@{u['username']}" if u and u["username"] else _name(u) if u else f"id {uid}")
            await bot.send_message(ADMIN_ID, f"{who} потратил {sp.total_amount} Stars: {STATUSES[key][2]}")
        except Exception:
            pass


# ---------------- админка ----------------

def admin_only(uid):
    return bool(ADMIN_ID) and uid == ADMIN_ID


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data="adm:stats"),
         InlineKeyboardButton(text="Топ игроков", callback_data="adm:rating")],
        [InlineKeyboardButton(text="Топ рефоводов", callback_data="adm:top")]])


def stats_text():
    t0 = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with closing(db()) as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        total = q("SELECT COUNT(*) FROM users")
        farmed = q("SELECT COUNT(*) FROM users WHERE last_claim=?", today())
        day = lambda k: q("SELECT COUNT(*) FROM users WHERE created>=? AND created<?", t0 - k * 86400, t0 - (k - 1) * 86400)
        new_today, new_yday, new_2d = q("SELECT COUNT(*) FROM users WHERE created>=?", t0), day(1), day(2)
        owned = {k: 0 for k in STATUSES}
        for r in c.execute("SELECT pets FROM users WHERE pets IS NOT NULL AND pets<>'{}' AND id<>?", (ADMIN_ID or 0,)):
            try:
                for k, n in json.loads(r["pets"]).items():
                    if k in owned:
                        owned[k] += int(n)
            except Exception:
                pass
        top = c.execute("SELECT id,username,first_name,points FROM users WHERE id<>? ORDER BY points DESC LIMIT 3",
                        (ADMIN_ID or 0,)).fetchall()
    lines = ["👑 Админ-панель Ферма", "",
             f"Юзеры: {total}", f"Фарм: {farmed}", f"Сегодня: {new_today}", f"Вчера: {new_yday}", f"Позавчера: {new_2d}", ""]
    lines += [f"{STATUSES[k][2]}: {n}" for k, n in owned.items()]
    lines += ["", "Топ 3 монеты:"]
    lines += [f"{i}. " + (f"@{r['username']}" if r["username"] else _name(r)) + f" — {r['points']:,.0f}".replace(",", " ")
              for i, r in enumerate(top, 1)] or ["Пока пусто"]
    return "\n".join(lines)


@dp.message(Command("admin"))
async def admin(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(stats_text())


def add_reward(title, price, stock, descr=""):
    with closing(db()) as c:
        cur = c.execute("INSERT INTO rewards(title,descr,price,stock,created) VALUES(?,?,?,?,?)",
                        (title.strip()[:80], descr.strip()[:300], int(price), int(stock), int(time.time())))
        c.commit()
        return cur.lastrowid


async def fulfill_order(oid):
    with closing(db()) as c:
        row = c.execute("SELECT * FROM reward_orders WHERE id=? AND status='pending'", (oid,)).fetchone()
        if not row:
            return None
        c.execute("UPDATE reward_orders SET status='done' WHERE id=?", (oid,))
        c.commit()
    try:
        await bot.send_message(row["user_id"], f"Твоя награда «{row['title']}» выдана")
    except Exception:
        pass
    return row


@dp.message(Command("addreward"))
async def cmd_addreward(m: Message, command: CommandObject):
    if not admin_only(m.from_user.id):
        return
    parts = [p.strip() for p in (command.args or "").split("|")]
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit() or int(parts[1]) <= 0 or int(parts[2]) <= 0:
        await m.answer("Использование: /addreward Название | цена в монетах | количество | описание (необязательно)\n"
                       "Пример: /addreward Коллекционный NFT #1 | 50000 | 10 | Ограниченный тираж")
        return
    rid = add_reward(parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else "")
    await m.answer(f"Награда #{rid} добавлена: «{parts[0]}», {parts[1]} монет, тираж {parts[2]}")


@dp.message(Command("rewards"))
async def cmd_rewards(m: Message):
    if not admin_only(m.from_user.id):
        return
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM rewards ORDER BY id").fetchall()
    text = "Награды:\n\n" + ("\n".join(f"#{r['id']} «{r['title']}» {r['price']} монет, осталось {r['stock']}" +
                                       ("" if r["active"] else " (снята)") for r in rows) or "Пока нет. /addreward")
    await m.answer(text + "\n\nСнять награду: /delreward НОМЕР")


PET_ALIAS = {"utya": "gold", "gold": "gold", "bunny": "platinum", "capybara": "platinum", "capy": "platinum", "platinum": "platinum",
             "cat": "diamond", "diamond": "diamond", "pepe": "obsidian", "obsidian": "obsidian"}


@dp.message(Command("grant"))
async def cmd_grant(m: Message, command: CommandObject):
    """/grant @username cat — подарить питомца (utya, capybara, cat, pepe). Если игрока ещё нет, выдастся при первом входе."""
    if not admin_only(m.from_user.id):
        return
    a = (command.args or "").split()
    key = PET_ALIAS.get(a[1].lower()) if len(a) == 2 else None
    if not key:
        await m.answer("Использование: /grant @username utya|capybara|cat|pepe")
        return
    name = a[0].lstrip("@").lower()
    with closing(db()) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT id FROM users WHERE lower(username)=?", (name,)).fetchone()
        if row:
            give_pet(c, row["id"], key)
        else:
            c.execute("INSERT OR REPLACE INTO pending_grants(username,pet) VALUES(?,?)", (name, key))
        c.commit()
    await m.answer(f"@{name}: {farm.TIERS[key]['name']} " + ("выдан" if row else "будет выдан при первом входе"))


@dp.message(Command("rewardprice"))
async def cmd_rewardprice(m: Message, command: CommandObject):
    if not admin_only(m.from_user.id):
        return
    a = (command.args or "").split()
    if len(a) != 2 or not a[0].isdigit() or not a[1].isdigit() or int(a[1]) <= 0:
        await m.answer("Использование: /rewardprice НОМЕР ЦЕНА_В_МОНЕТАХ\nНапример: /rewardprice 3 150000\n"
                       "Чтобы поменять цену всем сразу: /rewardprice all 150000")
        return
    with closing(db()) as c:
        c.execute("UPDATE rewards SET price=? WHERE id=?", (int(a[1]), int(a[0])))
        c.commit()
    await m.answer(f"Цена награды #{a[0]} теперь {a[1]} монет")


@dp.message(Command("rewardpriceall"))
async def cmd_rewardpriceall(m: Message, command: CommandObject):
    if not admin_only(m.from_user.id):
        return
    a = (command.args or "").strip()
    if not a.isdigit() or int(a) <= 0:
        await m.answer("Использование: /rewardpriceall ЦЕНА_В_МОНЕТАХ (для всех наград)")
        return
    with closing(db()) as c:
        c.execute("UPDATE rewards SET price=? WHERE active=1", (int(a),))
        c.commit()
    await m.answer(f"Цена всех активных наград теперь {a} монет")


@dp.message(Command("delreward"))
async def cmd_delreward(m: Message, command: CommandObject):
    if not admin_only(m.from_user.id):
        return
    a = (command.args or "").strip()
    if not a.isdigit():
        await m.answer("Использование: /delreward НОМЕР")
        return
    with closing(db()) as c:
        c.execute("UPDATE rewards SET active=0 WHERE id=?", (int(a),))
        c.commit()
    await m.answer(f"Награда #{a} снята с витрины")


@dp.callback_query(F.data.startswith("ord:"))
async def order_cb(q: CallbackQuery):
    if not admin_only(q.from_user.id):
        await q.answer()
        return
    row = await fulfill_order(int(q.data.split(":")[1]))
    await show(q, (q.message.text or "") + ("\n\nВыдано" if row else "\n\nУже была выдана"), None)
    try:
        await q.answer("Выдано" if row else "Уже выдана")
    except Exception:
        pass


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
        text = "Топ игроков (заработано монет за всё время):\n\n" + (
            "\n".join(f"{i}. {_name(r)}" + (f" (@{r['username']})" if r["username"] else "") + f": {r['season_points']:,.0f}"
                      for i, r in enumerate(rows, 1)) or "Пока пусто")
        await show(q, text, admin_kb())
    elif parts[1] == "orders":
        if len(parts) > 3 and parts[2] == "done":
            note = "Выдано" if await fulfill_order(int(parts[3])) else "Уже выдана"
        with closing(db()) as c:
            rows = c.execute("SELECT o.id,o.title,o.price,u.username,u.first_name,u.id uid FROM reward_orders o "
                             "JOIN users u ON u.id=o.user_id WHERE o.status='pending' ORDER BY o.id LIMIT 10").fetchall()
        if not rows:
            await show(q, "Заказов наград нет", admin_kb())
        else:
            text = "Заказы наград (нажми, чтобы отметить выданной):\n\n" + "\n".join(
                f"#{r['id']}: «{r['title']}» за {r['price']} монет, " + (f"@{r['username']}" if r["username"] else _name(r)) for r in rows)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"Выдано #{r['id']}", callback_data=f"adm:orders:done:{r['id']}")]
                                                       for r in rows] + [[InlineKeyboardButton(text="Назад", callback_data="adm:stats")]])
            await show(q, text, kb)
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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Фармить", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="Чат", url="https://t.me/peeppo_chat"),
         InlineKeyboardButton(text="Канал", url="https://t.me/peeppo_channel")]])
    text = ("<b>Привет, фермер! Добро пожаловать на ферму.</b>\n\n"
            "Нажимай <b>ФАРМ</b> и ферма вместе с питомцами будет приносить монеты круглые сутки.\n\n"
            "<b>Как играть</b>\n"
            "— Фарми раз в сутки\n"
            "— Покупай питомцев\n"
            "— Зови друзей и увеличивай доход\n"
            "— Копи монеты\n"
            "— Обменивай монеты на NFT")
    global WELCOME_FILE_ID
    try:
        sent = await m.answer_photo(WELCOME_FILE_ID or FSInputFile(os.path.join(HERE, "static", "welcome.png")),
                                    caption=text, reply_markup=kb, parse_mode="HTML")
        WELCOME_FILE_ID = sent.photo[-1].file_id
    except Exception as e:
        print("welcome photo:", e)
        await m.answer(text, reply_markup=kb, parse_mode="HTML")


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"), headers={"Cache-Control": "no-store"})
