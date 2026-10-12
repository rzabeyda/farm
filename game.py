"""Игровая логика Free Stars: очки, тапы с энергией, улучшения, пассивная прибыль, VIP-множители.

Очки - внутриигровая валюта, напрямую в Telegram Stars не обмениваются.
Telegram Stars выдаются только как призы сезона из заранее объявленного фонда.
"""
import json
import time

CHECKIN_BASE = 100          # очки за ежедневное «Я не бот»
STREAK_STEP = 10            # +10 очков за каждый день серии
STREAK_MAX_DAYS = 10        # серия растёт до 10 дней
TAP_BASE = 1
ENERGY_BASE = 500
REGEN_BASE = 1.0            # энергии в секунду
OFFLINE_CAP_H = 3           # пассивная прибыль копится не дольше 3 часов
MAX_TAPS_PER_REQ = 40
MAX_TAPS_PER_SEC = 12
REF_BONUS_INVITER = 1000    # один раз, когда друг впервые прошёл проверку «Я не бот»
REF_BONUS_FRIEND = 500

# VIP за Telegram Stars: ключ -> (цена в Stars, множитель всех получаемых очков, название)
STATUSES = {
    "gold": (25, 1.25, "Gold"),
    "platinum": (100, 1.5, "Platinum"),
    "diamond": (500, 2.0, "Diamond"),
    "obsidian": (1000, 3.0, "Obsidian"),
}

# Улучшения за очки: key -> dict
UPGRADES = {
    "tap":     dict(name="Сильный палец", kind="tap", base=200, step=1, growth=1.55, max=50, unit="очко за тап"),
    "energy":  dict(name="Запас энергии", kind="energy", base=150, step=100, growth=1.5, max=30, unit="к максимуму энергии"),
    "regen":   dict(name="Быстрая зарядка", kind="regen", base=300, step=0.5, growth=1.6, max=20, unit="энергии в секунду"),
    "dust":    dict(name="Звёздная пыль", kind="profit", base=400, step=30, growth=1.55, max=40, unit="очков в час"),
    "comet":   dict(name="Комета", kind="profit", base=1500, step=100, growth=1.55, max=40, unit="очков в час"),
    "cluster": dict(name="Скопление", kind="profit", base=5000, step=320, growth=1.55, max=40, unit="очков в час"),
    "galaxy":  dict(name="Галактика", kind="profit", base=18000, step=1100, growth=1.55, max=40, unit="очков в час"),
}


def upgrade_cost(key, level):
    u = UPGRADES[key]
    return int(u["base"] * (u["growth"] ** level))


def levels_of(row):
    try:
        d = json.loads(row["upg"] or "{}")
    except Exception:
        d = {}
    return {k: int(d.get(k, 0)) for k in UPGRADES}


def mult_of(status):
    return STATUSES[status][1] if status in STATUSES else 1.0


def stats(row):
    """Производные характеристики игрока (без учёта времени)."""
    lv = levels_of(row)
    tap = TAP_BASE + sum(UPGRADES[k]["step"] * lv[k] for k in UPGRADES if UPGRADES[k]["kind"] == "tap")
    max_energy = ENERGY_BASE + sum(UPGRADES[k]["step"] * lv[k] for k in UPGRADES if UPGRADES[k]["kind"] == "energy")
    regen = REGEN_BASE + sum(UPGRADES[k]["step"] * lv[k] for k in UPGRADES if UPGRADES[k]["kind"] == "regen")
    profit = sum(UPGRADES[k]["step"] * lv[k] for k in UPGRADES if UPGRADES[k]["kind"] == "profit")
    mult = mult_of(row["status"])
    return dict(levels=lv, tap=tap, max_energy=max_energy, regen=regen, profit=profit, mult=mult)


def current_energy(row, st, now):
    last = row["energy_ts"] or now
    e = row["energy"] if row["energy"] is not None else st["max_energy"]
    return min(st["max_energy"], e + max(0, now - last) * st["regen"])


def settle(c, uid, now=None):
    """Начисляет пассивную прибыль и восстанавливает энергию. Вызывать внутри транзакции."""
    now = now or time.time()
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    st = stats(row)
    energy = current_energy(row, st, now)
    gain = 0.0
    pts = row["profit_ts"]
    if pts:
        dt = min(max(0, now - pts), OFFLINE_CAP_H * 3600)
        gain = st["profit"] * st["mult"] * dt / 3600
    c.execute("UPDATE users SET points=points+?, season_points=season_points+?, energy=?, energy_ts=?, profit_ts=? WHERE id=?",
              (gain, gain, energy, now, now, uid))
    return gain


def checkin_reward(streak_after, mult):
    return int(round((CHECKIN_BASE + STREAK_STEP * (min(streak_after, STREAK_MAX_DAYS) - 1)) * mult))
