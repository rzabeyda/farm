"""Логика фермы Free Stars (v3).

Монеты - внутриигровая валюта. Питомцы покупаются за Telegram Stars (игровые предметы) и сами приносят монеты.
Монеты НЕ обмениваются на Telegram Stars: их можно потратить на ограниченные награды (NFT) в магазине.
"""
import time

OFFLINE_CAP_H = 8           # пассивный доход копится не дольше 8 часов без захода
STREAK_STEP = 0.05          # +5% к награде за ФАРМ за каждый день серии
STREAK_MAX_DAYS = 10
REF_BONUS_INVITER = 200     # монет, когда друг впервые нажал ФАРМ
REF_BONUS_FRIEND = 100

# Питомцы: stars - цена в Telegram Stars; res - что «приносит» (для описания), per_h - штук в час,
# price - монет за 1 шт. (ресурсы сразу превращаются в монеты), daily - награда за ФАРМ.
TIERS = {
    "farmer":   dict(stars=0,    name="Фермер",  tier="Новичок",  res="картошку", per_h=2,  price=1, daily=20),
    "gold":     dict(stars=25,   name="Уточка",  tier="Gold",     res="перья",    per_h=5,  price=2, daily=30),
    "platinum": dict(stars=100,  name="Зайка",   tier="Platinum", res="морковь",  per_h=12, price=3, daily=100),
    "diamond":  dict(stars=500,  name="Кошка",   tier="Diamond",  res="мышей",    per_h=30, price=5, daily=500),
    "obsidian": dict(stars=1000, name="Лягушка", tier="Obsidian", res="кувшинки", per_h=60, price=8, daily=1500),
}
PAID = [k for k in TIERS if k != "farmer"]


def coin_rate(tier):
    t = TIERS[tier]
    return t["per_h"] * t["price"]


def tier_of(row):
    return row["status"] if row["status"] in TIERS else "farmer"


def stats(row):
    k = tier_of(row)
    return dict(tier=k, t=TIERS[k], rate=coin_rate(k), cap_h=OFFLINE_CAP_H)


def settle(c, uid, now=None):
    """Начисляет пассивный доход монетами (не больше OFFLINE_CAP_H часов). Вызывать внутри транзакции."""
    now = now or time.time()
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    st = stats(row)
    last = row["res_ts"]
    gain = 0.0
    if last:
        gain = st["rate"] * min(max(0.0, now - last), OFFLINE_CAP_H * 3600) / 3600.0
    c.execute("UPDATE users SET points=points+?, season_points=season_points+?, res_ts=? WHERE id=?",
              (gain, gain, now, uid))
    return gain


def farm_reward(streak_after, daily):
    return int(round(daily * (1 + STREAK_STEP * (min(streak_after, STREAK_MAX_DAYS) - 1))))
