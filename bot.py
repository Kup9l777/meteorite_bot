import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

# -------------------- ЛОГИ --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("meteorite")

# -------------------- ENV --------------------
TG_TOKEN = os.getenv("TG_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
OZON_API_KEY = os.getenv("OZON_API_KEY", "")

# список юнитов для мониторинга (через .env)
WATCH_OFFERS = [s.strip() for s in os.getenv("WATCH_OFFERS", "").split(",") if s.strip()]
WATCH_PRODUCTS = [int(s) for s in os.getenv("WATCH_PRODUCTS", "").split(",") if s.strip().isdigit()]

# пороги/настройки
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "180"))  # 3 часа
HEARTBEAT_CHAT_ID = int(os.getenv("HEARTBEAT_CHAT_ID", ADMIN_IDS[0] if ADMIN_IDS else "0"))

POLL_PERIOD_SEC = int(os.getenv("POLL_PERIOD_SEC", "300"))      # 5 минут
CHANGE_CONFIRMS = int(os.getenv("CHANGE_CONFIRMS", "2"))        # сколькими циклами подтвердить
PRICE_TOLERANCE = int(os.getenv("PRICE_TOLERANCE", "1"))        # «погрешность» в рублях

# -------------------- BOT/DP --------------------
bot = Bot(token=TG_TOKEN, default_parse_mode=ParseMode.HTML)
dp = Dispatcher()

# -------------------- Состояние --------------------
last_activity: datetime = datetime.now()
last_cycle_at: Optional[datetime] = None
last_alert_at: Optional[datetime] = None

class PriceState:
    __slots__ = ("price", "confirm", "last_seen")
    def __init__(self, price: int):
        self.price = price
        self.confirm = 0
        self.last_seen = datetime.now()

# k: offer_id -> PriceState
buyer_prices: Dict[str, PriceState] = {}
no_marketing_now: int = 0

def touch_alive(note: str = "") -> None:
    global last_activity
    last_activity = datetime.now()
    if note:
        log.debug("alive: %s", note)

# -------------------- Ozon Seller API --------------------
async def get_ozon_products(limit: int = 100) -> List[dict]:
    """Возвращает items с offer_id/product_id, с фильтром по WATCH_* если задан."""
    url = "https://api-seller.ozon.ru/v3/product/list"
    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}

    all_items: List[dict] = []
    offset = 0

    async with aiohttp.ClientSession() as session:
        while True:
            payload = {"filter": {"visibility": "ALL"}, "limit": limit, "offset": offset}
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error("products %s: %s", resp.status, text)
                    break
                data = await resp.json()
                items = data.get("result", {}).get("items", [])
                if not items:
                    break

                if WATCH_OFFERS or WATCH_PRODUCTS:
                    items = [
                        it for it in items
                        if (not WATCH_OFFERS or it.get("offer_id") in WATCH_OFFERS)
                        and (not WATCH_PRODUCTS or it.get("product_id") in WATCH_PRODUCTS)
                    ]

                all_items.extend(items)
                if len(items) < limit:
                    break
                offset += limit

    touch_alive("ozon_products")
    return all_items


async def get_ozon_prices(offer_ids: List[str], product_ids: List[int]) -> Optional[dict]:
    """v5/product/info/prices — берём только marketing_price как «цену для покупателя»."""
    if not offer_ids and not product_ids:
        return {"items": []}

    url = "https://api-seller.ozon.ru/v5/product/info/prices"
    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}
    payload = {
        "cursor": "",
        "filter": {"offer_id": offer_ids, "product_id": product_ids, "visibility": "ALL"},
        "limit": 100,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            text = await resp.text()
            if resp.status != 200:
                log.error("prices %s: %s", resp.status, text)
                return None
            data = await resp.json()
            touch_alive("ozon_prices")
            return data

def pick_buyer_price(item: dict) -> Optional[int]:
    """Цена для покупателя — строго marketing_price; если нет/0 — вернём None."""
    p = item.get("price") or {}
    mp = p.get("marketing_price")
    try:
        mp = int(mp) if mp is not None else None
    except Exception:
        mp = None
    return mp if mp and mp > 0 else None

def arrow(old: int, new: int) -> str:
    return "↑" if new > old else "↓"

# -------------------- Команды --------------------
HELP_TEXT = (
    "Привет! Я показываю цены для покупателя (с учётом маркетинговых скидок).\n\n"
    "Команды:\n"
    "/prices — текущие цены\n"
    "/monitor — включить мониторинг (уведомлю об изменениях)\n"
    "/health — состояние бота и мониторинга"
)

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(HELP_TEXT)
    touch_alive("cmd_start")

@dp.message(Command("prices"))
async def cmd_prices(message: Message):
    products = await get_ozon_products()
    if not products:
        await message.answer("Не удалось получить список товаров.")
        return

    offer_ids = [it.get("offer_id") for it in products if it.get("offer_id")]
    product_ids = [it.get("product_id") for it in products if it.get("product_id")]

    data = await get_ozon_prices(offer_ids, product_ids)
    if not data:
        await message.answer("Ошибка при запросе цен.")
        return

    lines = ["Текущие цены для покупателя:"]
    miss = 0
    for it in data.get("items", []):
        offer = it.get("offer_id")  # используем как «читаемое имя»
        buyer = pick_buyer_price(it)
        if buyer is None:
            miss += 1
            lines.append(f"• {offer}: — (нет маркетинговой цены)")
        else:
            lines.append(f"• {offer}: {buyer} ₽")

    lines.append(f"\nНедоступна маркетинговая цена: {miss} шт.")
    await message.answer("\n".join(lines))
    touch_alive("cmd_prices")

@dp.message(Command("health"))
async def cmd_health(message: Message):
    now = datetime.now()
    silence = int((now - last_activity).total_seconds() // 60)
    last_cycle_txt = "-" if not last_cycle_at else f"{int((now - last_cycle_at).total_seconds() // 60)} мин назад"
    last_alert_txt = "-" if not last_alert_at else f"{int((now - last_alert_at).total_seconds() // 60)} мин назад"

    text = (
        "Состояние:\n"
        f"• Тишина: {silence} мин (порог {HEARTBEAT_MINUTES})\n"
        f"• Последний цикл: {last_cycle_txt}\n"
        f"• Последнее уведомление: {last_alert_txt}\n"
        f"• В кэше цен: {len(buyer_prices)} офферов\n"
        f"• Сейчас без marketing_price: {no_marketing_now}"
    )
    await message.answer(text)
    touch_alive("cmd_health")

@dp.message(Command("monitor"))
async def cmd_monitor(message: Message):
    await message.answer("Мониторинг уже работает в фоне. Сообщу, если цена для покупателя изменится устойчиво.")
    touch_alive("cmd_monitor")

# ---------- дружелюбный ответ на любое обычное сообщение ----------
@dp.message(F.text & ~F.text.startswith("/"))
async def greet_any_text(message: Message):
    await message.answer(HELP_TEXT)
    touch_alive("greet_any")

# -------------------- Мониторинг --------------------
async def check_prices_periodically():
    """Следим только за marketing_price. Оповещаем сразу при фактическом изменении."""
    global last_cycle_at, last_alert_at, no_marketing_now

    while True:
        try:
            # 1) Берём список отслеживаемых товаров
            products = await get_ozon_products()
            offer_ids = [it.get("offer_id") for it in products if it.get("offer_id")]
            product_ids = [it.get("product_id") for it in products if it.get("product_id")]

            # 2) Тянем цены
            data = await get_ozon_prices(offer_ids, product_ids)
            if not data:
                await asyncio.sleep(POLL_PERIOD_SEC)
                continue

            # 3) Собираем наблюдения по marketing_price
            no_marketing_now = 0
            observed: Dict[str, int] = {}
            for it in data.get("items", []):
                offer = it.get("offer_id")
                buyer = pick_buyer_price(it)  # только marketing_price (>0), иначе None
                if buyer is None:
                    no_marketing_now += 1
                    continue
                observed[offer] = buyer

            # 4) Вычисляем изменения (без дебаунса/толеранса)
            changes: List[str] = []
            now = datetime.now()

            for offer, cur_price in observed.items():
                state = buyer_prices.get(offer)

                # первый раз видим — просто запоминаем
                if state is None:
                    buyer_prices[offer] = PriceState(cur_price)
                    continue

                # обновляем метку последнего наблюдения
                state.last_seen = now

                # если цена реально изменилась — фиксируем и сообщаем
                if cur_price != state.price:
                    prev = state.price
                    state.price = cur_price
                    # формируем красивую строку-элемент списка
                    changes.append(f"• {offer}: {prev} ₽ → {cur_price} ₽ {arrow(prev, cur_price)}")

            # 5) Отправляем единым сообщением
            if changes:
                last_alert_at = datetime.now()
                text = "Цены изменились по следующим товарам:\n" + "\n".join(changes)
                for admin in ADMIN_IDS:
                    try:
                        await bot.send_message(admin, text)
                    except Exception as e:
                        log.warning("send to %s failed: %s", admin, e)

            last_cycle_at = datetime.now()
            touch_alive("cycle_ok")

        except Exception as e:
            log.exception("periodic error: %s", e)

        await asyncio.sleep(POLL_PERIOD_SEC)

# -------------------- Heartbeat --------------------
async def heartbeat_watcher():
    global last_activity
    threshold = timedelta(minutes=HEARTBEAT_MINUTES)
    if not HEARTBEAT_CHAT_ID:
        log.warning("HEARTBEAT_CHAT_ID not set — heartbeat тихий.")
        return

    while True:
        try:
            silence = datetime.now() - last_activity
            if silence > threshold:
                try:
                    await bot.send_message(
                        HEARTBEAT_CHAT_ID,
                        f"⚠️ Тишина: нет активности {int(silence.total_seconds() // 60)} мин (порог {HEARTBEAT_MINUTES})."
                    )
                except Exception as e:
                    log.warning("heartbeat send failed: %s", e)
                touch_alive("heartbeat_alert")
        except Exception as e:
            log.error("heartbeat error: %s", e)

        await asyncio.sleep(60)

# -------------------- ENTRYPOINT --------------------
async def main():
    asyncio.create_task(check_prices_periodically())
    asyncio.create_task(heartbeat_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
