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

STREAK_STEP = 1                # +монет в сутки за каждый день серии ФАРМ подряд
REF_MIN_SECONDS = 10           # проверка «живой человек»: первый ФАРМ друга засчитывается, только если прошло не меньше стольких секунд после его первого входа
REF_BONUS = 50                # монет приглашающему, когда друг нажал ФАРМ: сразу в первый раз и потом в каждый день, когда друг играет
REF_CAP = 50                 # столько друзей в день приносят бонус (защита от накрутки аккаунтами)

# Питомцы: stars - цена в Telegram Stars, rate - монет в час (у питомца ровно один показатель дохода).
# Баланс: доход на 1 Star растёт с ценой питомца в пропорции 30 : 27 : 24 : 21 (Utya : Capybara : Cat : Pepe),
# то есть Pepe при той же цене даёт в 30/21 = 1.43 раза больше монет, чем 40 Utya. Pepe = 480 монет в час (11 520 в сутки),
# остальные считаются от него: rate = 480 / stars_pepe * stars * (21 / дней).
TIERS = {
    "farmer":   dict(stars=0,    name="Фермер", rate=2),
    "gold":     dict(stars=25,   name="Utya",   rate=8.4),             # 201.6 в сутки, 8.064 на Star
    "platinum": dict(stars=100,  name="Capybara", rate=37.333333333),    # 896 в сутки, 8.96 на Star
    "diamond":  dict(stars=500,  name="Scared Cat", short="Cat", rate=210),   # 5 040 в сутки, 10.08 на Star
    "obsidian": dict(stars=1000, name="Pepe",   rate=480),             # 11 520 в сутки, 11.52 на Star
}
PAID = [k for k in TIERS if k != "farmer"]


COIN_PAYBACK_DAYS = 20          # питомца можно купить за монеты: цена = его доход за столько суток (округляется до сотен)


def coin_price(tier):
    """Цена питомца в монетах (0 для Фермера, его не покупают)."""
    if TIERS[tier]["stars"] == 0:
        return 0
    return int(round(TIERS[tier]["rate"] * 24 * COIN_PAYBACK_DAYS, -2))


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


def streak_rate(streak):
    """Серия ФАРМ: каждый день подряд добавляет +1 монету в сутки к доходу (в час это 1/24)."""
    return max(0, int(streak or 0)) * STREAK_STEP / 24.0


def tier_of(row):
    return top_pet(pets_of(row))


def stats(row):
    pets = pets_of(row)
    k = top_pet(pets)
    return dict(tier=k, t=TIERS[k], rate=rate_of(pets) + streak_rate(row["streak"]), pets=pets)


def farm_until_for(now=None):
    """Конец следующих суток по UTC: ФАРМ в любой момент дня D держит ферму включённой до 00:00 дня D+2."""
    now = now or time.time()
    d = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (d + timedelta(days=2)).timestamp()


def settle(c, uid, now=None):
    """Начисляет монеты за время, пока ферма была включена (до farm_until). Вызывать внутри транзакции."""
    now = now or time.time()
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    st = stats(row)
    last = row["res_ts"]
    until = row["farm_until"] or 0
    gain = 0.0
    end = min(now, until)
    if last and end > last:
        gain = st["rate"] * (end - last) / 3600.0
    c.execute("UPDATE users SET points=points+?, season_points=season_points+?, res_ts=? WHERE id=?",
              (gain, gain, now, uid))
    return gain
