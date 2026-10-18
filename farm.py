"""Логика фермы Free Stars.

Монеты и ресурсы - внутриигровые. Монеты НЕ обмениваются на Telegram Stars.
Питомцы покупаются за Telegram Stars (это покупка игровых предметов); реальные Stars
игроки могут получать только как призы сезона из заранее объявленного фонда.
"""
import json
import time

OFFLINE_BASE_H = 8          # сколько часов ферма копит ресурсы без захода
STREAK_STEP = 0.05          # +5% к награде за ФАРМ за каждый день серии
STREAK_MAX_DAYS = 10
REF_BONUS_INVITER = 200     # монет, когда друг впервые нажал ФАРМ
REF_BONUS_FRIEND = 100

# Питомцы. key -> (цена в Telegram Stars, название питомца, уровень, ресурс, производство в час, цена продажи 1 шт., награда за ФАРМ)
# farmer - бесплатный стартовый уровень.
TIERS = {
    "farmer":   dict(stars=0,    name="Нищий фермер", tier="Новичок",  res="картошка", per_h=2,  price=1, daily=20),
    "gold":     dict(stars=25,   name="Уточка",       tier="Gold",     res="перья",    per_h=5,  price=2, daily=30),
    "platinum": dict(stars=100,  name="Зайка",        tier="Platinum", res="морковь",  per_h=12, price=3, daily=100),
    "diamond":  dict(stars=500,  name="Кошка",        tier="Diamond",  res="мыши",     per_h=30, price=5, daily=500),
    "obsidian": dict(stars=1000, name="Лягушка",      tier="Obsidian", res="кувшинки", per_h=60, price=8, daily=1500),
}
PAID = [k for k in TIERS if k != "farmer"]

# Улучшения фермы за монеты
UPGRADES = {
    "barn":   dict(name="Амбар", desc="+2 часа хранения ресурсов", base=100, growth=1.6, max=10, step=2),
    "feed":   dict(name="Кормушка", desc="+10% к производству", base=150, growth=1.55, max=20, step=0.10),
    "market": dict(name="Рынок", desc="+5% к цене продажи", base=200, growth=1.55, max=20, step=0.05),
}


def upgrade_cost(key, level):
    u = UPGRADES[key]
    return int(u["base"] * (u["growth"] ** level))


def tier_of(row):
    return row["status"] if row["status"] in TIERS else "farmer"


def levels_of(row):
    try:
        d = json.loads(row["upg"] or "{}")
    except Exception:
        d = {}
    return {k: int(d.get(k, 0)) for k in UPGRADES}


def stats(row):
    t = TIERS[tier_of(row)]
    lv = levels_of(row)
    cap_h = OFFLINE_BASE_H + UPGRADES["barn"]["step"] * lv["barn"]
    rate = t["per_h"] * (1 + UPGRADES["feed"]["step"] * lv["feed"])
    price = t["price"] * (1 + UPGRADES["market"]["step"] * lv["market"])
    return dict(levels=lv, tier=tier_of(row), t=t, cap_h=cap_h, rate=rate, price=price, cap=rate * cap_h)


def settle(c, uid, now=None):
    """Дописывает произведённые ресурсы на склад (не больше ёмкости). Вызывать внутри транзакции."""
    now = now or time.time()
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    st = stats(row)
    last = row["res_ts"] or now
    res = row["res"] or 0.0
    res = min(st["cap"], res + st["rate"] * max(0.0, now - last) / 3600.0)
    c.execute("UPDATE users SET res=?, res_ts=? WHERE id=?", (res, now, uid))
    return res


def farm_reward(streak_after, daily):
    return int(round(daily * (1 + STREAK_STEP * (min(streak_after, STREAK_MAX_DAYS) - 1))))
