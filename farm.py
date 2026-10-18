"""Логика фермы (v4).

Один доход: монеты в час. Ферма работает круглые сутки, пока игрок раз в сутки заходит и нажимает ФАРМ:
каждое нажатие ФАРМ продлевает работу фермы до конца следующих суток (UTC). Пропустил день, ферма останавливается
до следующего ФАРМ.
Монеты - внутриигровая валюта. Питомцы покупаются за Telegram Stars (игровые предметы).
Монеты НЕ обмениваются на Telegram Stars: их можно потратить на ограниченные награды (NFT) в магазине.
"""
import json
import time
from datetime import datetime, timedelta, timezone

REF_RATE = 2                # монет в час от каждого активного друга (друг заходит и нажимает ФАРМ каждый день)
REF_CAP = 20                # учитываются не больше стольких друзей одновременно (защита от накрутки)

# Питомцы: stars - цена в Telegram Stars, rate - монет в час (у питомца ровно один показатель дохода).
# Баланс: доход на 1 Star растёт с ценой питомца в пропорции 30 : 27 : 24 : 21 (Utya : Bunny : Cat : Pepe),
# то есть Pepe при той же цене даёт в 30/21 = 1.43 раза больше монет, чем 40 Utya. Pepe = 480 монет в час (11 520 в сутки),
# остальные считаются от него: rate = 480 / stars_pepe * stars * (21 / дней).
TIERS = {
    "farmer":   dict(stars=0,    name="Фермер", rate=2),
    "gold":     dict(stars=25,   name="Utya",   rate=8.4),             # 201.6 в сутки, 8.064 на Star
    "platinum": dict(stars=100,  name="Bunny",  rate=37.333333333),    # 896 в сутки, 8.96 на Star
    "diamond":  dict(stars=500,  name="Scared Cat", short="Cat", rate=210),   # 5 040 в сутки, 10.08 на Star
    "obsidian": dict(stars=1000, name="Pepe",   rate=480),             # 11 520 в сутки, 11.52 на Star
}
PAID = [k for k in TIERS if k != "farmer"]


def coin_rate(tier):
    return TIERS[tier]["rate"]


def pets_of(row):
    """Питомцы игрока: {ключ: сколько штук}. Фермер есть у всех и сюда не входит. Купить можно любых и по нескольку."""
    try:
        d = json.loads(row["pets"] or "{}")
    except Exception:
        d = {}
    return {k: int(d.get(k, 0)) for k in PAID if int(d.get(k, 0)) > 0}


def top_pet(pets):
    for k in reversed(PAID):
        if pets.get(k):
            return k
    return "farmer"


def rate_of(pets):
    """Доход в час: доход фермера плюс доход каждого купленного питомца (складывается)."""
    return coin_rate("farmer") + sum(n * coin_rate(k) for k, n in pets.items())


def tier_of(row):
    return top_pet(pets_of(row))


def stats(row):
    pets = pets_of(row)
    k = top_pet(pets)
    return dict(tier=k, t=TIERS[k], rate=rate_of(pets), pets=pets)


def farm_until_for(now=None):
    """Конец следующих суток по UTC: ФАРМ в любой момент дня D держит ферму включённой до 00:00 дня D+2."""
    now = now or time.time()
    d = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (d + timedelta(days=2)).timestamp()


def settle(c, uid, now=None):
    """Начисляет монеты за время, пока ферма была включена (до farm_until): доход питомца плюс доход от друзей.
    Друг приносит REF_RATE в час, пока его ферма включена (он нажимает ФАРМ каждый день); считается не больше REF_CAP друзей.
    Вызывать внутри транзакции."""
    now = now or time.time()
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    st = stats(row)
    last = row["res_ts"]
    until = row["farm_until"] or 0
    gain = 0.0
    end = min(now, until)
    if last and end > last:
        gain = st["rate"] * (end - last) / 3600.0
        overlaps = []
        for f in c.execute("SELECT farm_until FROM users WHERE referrer_id=? AND ref_active=1 AND farm_until>?", (uid, last)):
            fu = f["farm_until"]
            # окно друга: сутки нажатия ФАРМ и следующие сутки (см. farm_until_for)
            ov = min(end, fu) - max(last, fu - 2 * 86400)
            if ov > 0:
                overlaps.append(ov)
        overlaps.sort(reverse=True)
        gain += REF_RATE * sum(overlaps[:REF_CAP]) / 3600.0
    c.execute("UPDATE users SET points=points+?, season_points=season_points+?, res_ts=? WHERE id=?",
              (gain, gain, now, uid))
    return gain


def active_friends(c, uid, now=None):
    """Сколько друзей сейчас приносят монеты (их ферма включена)."""
    now = now or time.time()
    return c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=? AND ref_active=1 AND farm_until>?", (uid, now)).fetchone()[0]
