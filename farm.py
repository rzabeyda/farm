"""Логика фермы (v4).

Один доход: монеты в час. Ферма работает круглые сутки, пока игрок раз в сутки заходит и нажимает ФАРМ:
каждое нажатие ФАРМ продлевает работу фермы до конца следующих суток (UTC). Пропустил день, ферма останавливается
до следующего ФАРМ.
Монеты - внутриигровая валюта. Питомцы покупаются за Telegram Stars (игровые предметы).
Монеты НЕ обмениваются на Telegram Stars: их можно потратить на ограниченные награды (NFT) в магазине.
"""
import time
from datetime import datetime, timedelta, timezone

REF_BONUS_INVITER = 200     # монет, когда друг впервые нажал ФАРМ
REF_BONUS_FRIEND = 100

# Питомцы: stars - цена в Telegram Stars; per_h - сколько штук приносит в час, price - монет за штуку.
# Для игрока это один показатель: монет в час (coin_rate).
TIERS = {
    "farmer":   dict(stars=0,    name="Фермер",      per_h=2,  price=1),
    "gold":     dict(stars=25,   name="Utya",        per_h=5,  price=2),
    "platinum": dict(stars=100,  name="Bunny",       per_h=12, price=3),
    "diamond":  dict(stars=500,  name="Scared Cat", short="Cat", per_h=30, price=5),
    "obsidian": dict(stars=1000, name="Pepe",        per_h=60, price=8),
}
PAID = [k for k in TIERS if k != "farmer"]


def coin_rate(tier):
    t = TIERS[tier]
    return t["per_h"] * t["price"]


def tier_of(row):
    return row["status"] if row["status"] in TIERS else "farmer"


def stats(row):
    k = tier_of(row)
    return dict(tier=k, t=TIERS[k], rate=coin_rate(k))


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
