import os
import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

# -------------------- ЛОГИ --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("meteorite")

# -------------------- ENV --------------------
TG_TOKEN = os.getenv("TG_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
OZON_API_KEY = os.getenv("OZON_API_KEY", "")

# heartbeat-параметры
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))
HEARTBEAT_CHAT_ID = int(os.getenv("HEARTBEAT_CHAT_ID", ADMIN_IDS[0] if ADMIN_IDS else "0"))

# -------------------- BOT/DP --------------------
bot = Bot(token=TG_TOKEN, default_parse_mode=ParseMode.HTML)
dp = Dispatcher()

# -------------------- Состояние --------------------
# последняя «живая» активность (входящее сообщение / успешный тик цикла / удачный запрос к Ozon)
last_activity: datetime = datetime.now()

# кэш последних цен для определения изменений
previous_prices: dict[str, int] = {}

# время последнего УДАЧНОГО тика периодической задачи (для /health)
last_cycle_at: datetime | None = None

# -------------------- Утилита обновления активности --------------------
def touch_alive(note: str = "") -> None:
    """
    Обновить флаг «бот жив».
    Вызывается:
      - при любом входящем сообщении
      - при успешном запросе к Ozon API
      - в конце каждого успешного тика периодической задачи
    """
    global last_activity
    last_activity = datetime.now()
    if note:
        log.debug("heartbeat touch: %s", note)

# -------------------- Ozon API --------------------
async def get_ozon_products(limit: int = 100) -> list[dict]:
    """
    Возвращает список товаров (items) с offer_id/product_id из Ozon.
    На УСПЕХ — touch_alive().
    """
    url = "https://api-seller.ozon.ru/v3/product/list"
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "filter": {"visibility": "ALL"},
        "limit": limit,
        "offset": 0,
    }

    all_items: list[dict] = []
    async with aiohttp.ClientSession() as session:
        while True:
            async with session.post(url, headers=headers, json=payload) as resp:
                text = await resp.text()
                if resp.status != 200:
                    log.error("Failed to fetch products: status=%s, text=%s", resp.status, text)
                    return []  # не обновляем heartbeat на неуспех

                data = await resp.json()
                items = data.get("result", {}).get("items", [])
                all_items.extend(items)

                # успех запроса -> считаем активность
                touch_alive("ozon_products")

                if not items:
                    break
                if len(items) < limit:
                    break

                payload["offset"] += len(items)

    return all_items


async def get_ozon_prices(offer_ids: list[str], product_ids: list[int]) -> dict | None:
    """
    Возвращает структуру с ценами по offer_id/product_id.
    На УСПЕХ — touch_alive().
    """
    url = "https://api-seller.ozon.ru/v5/product/info/prices"
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "with_discount": True,
        "filter": {
            "offer_id": offer_ids,
            "product_id": product_ids,
            "visibility": "ALL",
        },
        "limit": 100,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            text = await resp.text()
            if resp.status != 200:
                log.error("Failed to fetch prices: status=%s, text=%s", resp.status, text)
                return None

            # успех запроса -> считаем активность
            touch_alive("ozon_prices")
            return await resp.json()

# -------------------- Команды --------------------
@dp.message(Command("start"))
async def start_cmd(message: Message):
    await message.answer(
    "Привет! Я бот.\n"
    "Используй /prices для проверки цен.\n"
    "Набери: /health и увидишь текущий статус работоспособности бота,\n"
    "а также когда был последний успешный запрос проверки цен.")
    touch_alive("cmd_start")

@dp.message(Command("prices"))
async def prices_cmd(message: Message):
    products = await get_ozon_products()
    if not products:
        await message.answer("Не удалось получить список товаров с Ozon.")
        return

    offer_ids = [it.get("offer_id") for it in products if it.get("offer_id")]
    product_ids = [it.get("product_id") for it in products if it.get("product_id")]

    prices = await get_ozon_prices(offer_ids, product_ids)
    if not prices:
        await message.answer("Ошибка при запросе цен к Ozon API.")
        return

    # короткая сводка (первые N)
    lines = []
    for it in prices.get("items", [])[:10]:
        offer = it.get("offer_id")
        product = it.get("product_id")
        marketing_price = it.get("marketing_price") or 0
        # price может быть как числом, так и объектом {price: "..."}
        if isinstance(it.get("price"), (int, float)):
            regular_price = it.get("price") or 0
        else:
            regular_price = (it.get("price") or {}).get("price", 0)
        price = marketing_price if marketing_price > 0 else regular_price

        old_p = it.get("old_price") or 0
        discount = old_p - (price or 0)
        suffix = f" (скидка {discount} руб.)" if discount and discount > 0 else ""
        lines.append(f"• {offer} (Product ID: {product}): Цена {price} руб.{suffix}")

    await message.answer("\n".join(lines) if lines else "Цены не найдены.")
    touch_alive("cmd_prices")

# ✅ Новая команда — проверка состояния мониторинга
@dp.message(Command(commands=["health", "ping", "monitor"]))
async def health_cmd(message: Message):
    now = datetime.now()
    silence_td = now - last_activity
    silence_min = silence_td.total_seconds() // 60
    threshold = timedelta(minutes=HEARTBEAT_MINUTES)

    # статус по «тишине»
    if silence_td <= threshold:
        hb_status = "OK"
    elif silence_td <= threshold + timedelta(minutes=HEARTBEAT_MINUTES):
        hb_status = "WARN"
    else:
        hb_status = "SILENT"

    # статус по последнему тiku цикла
    cycle_status = "—"
    last_cycle_txt = "никогда"
    if last_cycle_at:
        since_cycle = now - last_cycle_at
        last_cycle_txt = f"{int(since_cycle.total_seconds() // 60)} мин назад"
        cycle_status = "OK" if since_cycle <= timedelta(minutes=15) else "STALE"

    text = (
        f"🩺 Проверка работоспособности бота\n"
        f"• Проверка работоспособности бота: {hb_status} (тишина {int(silence_min)} мин, порог {HEARTBEAT_MINUTES})\n"
        f"• Отклик от Ozon: {cycle_status} ({last_cycle_txt})\n"
        f"• Админы_бота: {', '.join(map(str, ADMIN_IDS)) or '—'}"
    )
    await message.answer(text)
    touch_alive("cmd_health")

# -------------------- Периодическая проверка цен --------------------
async def check_prices_periodically():
    """
    Периодическая задача: запрашивает цены и отправляет уведомления об изменениях.
    По успешному ТИКУ — touch_alive().
    """
    global previous_prices, last_cycle_at
    while True:
        try:
            products = await get_ozon_products()
            if not products:
                # на неуспех не трогаем heartbeat — пусть сработает алерт тишины
                await asyncio.sleep(60)
                continue

            offer_ids = [it.get("offer_id") for it in products if it.get("offer_id")]
            product_ids = [it.get("product_id") for it in products if it.get("product_id")]

            prices = await get_ozon_prices(offer_ids, product_ids)
            if not prices:
                await asyncio.sleep(60)
                continue

            # разбор цен и оповещение об изменениях
            for it in prices.get("items", []):
                offer = it.get("offer_id")

                marketing_price = it.get("marketing_price") or 0
                if isinstance(it.get("price"), (int, float)):
                    regular_price = it.get("price") or 0
                else:
                    regular_price = (it.get("price") or {}).get("price", 0)
                price = marketing_price if marketing_price > 0 else regular_price

                old_price = previous_prices.get(offer)
                if old_price is not None and old_price != price:
                    text = f"Цена изменилась для <b>{offer}</b>: {old_price} → {price} руб."
                    for admin in ADMIN_IDS:
                        try:
                            await bot.send_message(admin, text)
                        except Exception as e:
                            log.warning("send to admin %s failed: %s", admin, e)

                previous_prices[offer] = price

            # успешный тик цикла — жив
            last_cycle_at = datetime.now()
            touch_alive("cycle_tick")

        except Exception as e:
            log.exception("periodic error: %s", e)

        # период проверки
        await asyncio.sleep(300)  # 5 минут

# -------------------- Heartbeat-монитор --------------------
async def heartbeat_watcher():
    """
    Если нет активности (сообщения/успешные тики/успешные запросы) дольше HEARTBEAT_MINUTES — шлём алерт.
    После алерта делаем touch_alive, чтобы не спамить каждую минуту.
    """
    global last_activity
    threshold = timedelta(minutes=HEARTBEAT_MINUTES)

    if not HEARTBEAT_CHAT_ID:
        log.warning("HEARTBEAT_CHAT_ID not set — heartbeat будет тихим.")
        return

    while True:
        try:
            silence = datetime.now() - last_activity
            if silence > threshold:
                try:
                    await bot.send_message(
                        HEARTBEAT_CHAT_ID,
                        (
                            f"⚠️ <b>Тишина</b>: нет активности "
                            f"{int(silence.total_seconds() // 60)} мин (порог {HEARTBEAT_MINUTES})."
                        ),
                    )
                except Exception as e:
                    log.warning("send heartbeat alert failed: %s", e)

                # чтобы не срабатывать каждую минуту, «обнулим» счётчик
                touch_alive("heartbeat_alert")
        except Exception as e:
            log.error("heartbeat watcher error: %s", e)

        await asyncio.sleep(60)

# -------------------- Обновление активности на КАЖДОЕ сообщение --------------------
@dp.message()  # ловим всё остальное
async def any_message(msg: Message):
    touch_alive("incoming_msg")
    # опционально ничего не отвечаем, чтоб не засорять чат

# -------------------- ENTRYPOINT --------------------
async def main():
    # запускаем фоновые задачи
    asyncio.create_task(check_prices_periodically())
    asyncio.create_task(heartbeat_watcher())

    # и сам бот
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
