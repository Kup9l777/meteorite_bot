import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional

import aiohttp
from aiogram import Bot, Dispatcher
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

# Что мониторить: списки через запятую (необязательны)
MONITOR_OFFER_IDS = [x.strip() for x in os.getenv("MONITOR_OFFER_IDS", "").split(",") if x.strip()]
MONITOR_PRODUCT_IDS = [int(x) for x in os.getenv("MONITOR_PRODUCT_IDS", "").split(",") if x.strip()]

# Порог «тишины»
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "180"))
HEARTBEAT_CHAT_ID = int(os.getenv("HEARTBEAT_CHAT_ID", ADMIN_IDS[0] if ADMIN_IDS else "0"))

# -------------------- BOT/DP --------------------
bot = Bot(token=TG_TOKEN, default_parse_mode=ParseMode.HTML)
dp = Dispatcher()

# -------------------- Состояние --------------------
last_activity: datetime = datetime.now()      # последняя любая активность
last_cycle_at: Optional[datetime] = None      # последний удачный цикл мониторинга
previous_prices: Dict[str, int] = {}          # кэш цен (ключ — offer_id)
STARTED_AT = datetime.now()

# -------------------- Утилиты --------------------
def touch_alive(note: str = "") -> None:
    global last_activity
    last_activity = datetime.now()
    if note:
        log.debug("touch: %s", note)

def pick_buyer_price(item: dict) -> Tuple[int, int, int]:
    """
    Возвращаем (price, old_price, discount_percent) для покупателя.
    В ответах Seller API свежая цена для покупателя хранится в 'price.marketing_price' (если >0),
    иначе используем 'price.price'. old_price — 'price.old_price'.
    """
    p = item.get("price", {}) if isinstance(item.get("price"), dict) else {}
    marketing_price = _to_int(p.get("marketing_price"))
    regular_price   = _to_int(p.get("price"))
    old_price       = _to_int(p.get("old_price"))
    price = marketing_price if marketing_price > 0 else regular_price
    discount = max(old_price - price, 0) if old_price and price else 0
    discount_pct = int(round(discount * 100 / old_price)) if old_price else 0
    return price, old_price, discount_pct

def _to_int(v) -> int:
    try:
        if v is None: return 0
        if isinstance(v, (int, float)): return int(round(v))
        return int(round(float(str(v).replace(",", "."))))
    except Exception:
        return 0

def chunks(lines: List[str], max_len: int = 4000) -> List[str]:
    """Разбиваем список строк на куски по лимиту Telegram."""
    blocks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > max_len:
            blocks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        blocks.append(cur)
    return blocks

def human_health(now: datetime) -> Tuple[str, str]:
    """Вернём (статус_тишины, текст_последнего_цикла)."""
    silence_td = now - last_activity
    threshold = timedelta(minutes=HEARTBEAT_MINUTES)

    if silence_td <= threshold:
        hb = "OK"
    elif silence_td <= threshold + timedelta(minutes=HEARTBEAT_MINUTES):
        hb = "WARN"
    else:
        hb = "SILENT"

    if last_cycle_at:
        minutes = int((now - last_cycle_at).total_seconds() // 60)
        last_text = f"{minutes} мин назад"
    else:
        last_text = "никогда"
    return hb, last_text

def format_price_line(title: str, product_id: int, price: int, old_price: int, discount_pct: int) -> str:
    """
    Красивый пункт отчёта:
    • <b>название</b> (ID 123): 327 ₽ — скидка 1 372 ₽ (81%)
    """
    if old_price and price and old_price > price:
        discount_abs = old_price - price
        return f"• {title}: {price} ₽"
    return f"• <b>{title}</b> (ID {product_id}): {price} ₽"

def filter_to_monitored(items: List[dict]) -> List[dict]:
    """
    Оставляем только то, что прописано в .env.
    Если списки пустые — вернём исходный список (мониторим всё).
    """
    if not MONITOR_OFFER_IDS and not MONITOR_PRODUCT_IDS:
        return items
    out = []
    for it in items:
        offer = str(it.get("offer_id") or "")
        pid = it.get("product_id")
        if (MONITOR_OFFER_IDS and offer in MONITOR_OFFER_IDS) or (MONITOR_PRODUCT_IDS and pid in MONITOR_PRODUCT_IDS):
            out.append(it)
    return out

# -------------------- Ozon Seller API --------------------
async def get_ozon_products(limit: int = 100) -> List[dict]:
    """
    Получаем (offer_id, product_id, name) — пригодится для заголовков.
    """
    url = "https://api-seller.ozon.ru/v3/product/list"
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }

    all_items: List[dict] = []
    offset = 0
    async with aiohttp.ClientSession() as session:
        while True:
            payload = {"filter": {"visibility": "ALL"}, "limit": limit, "offset": offset}
            async with session.post(url, headers=headers, json=payload) as resp:
                text = await resp.text()
                if resp.status != 200:
                    log.error("products %s: %s", resp.status, text)
                    return []
                data = await resp.json()
                items = data.get("result", {}).get("items", []) or []
                if not items:
                    break
                all_items.extend(items)
                offset += len(items)
                if len(items) < limit:
                    break
    touch_alive("ozon_products")
    return all_items

async def get_ozon_prices(offer_ids: List[str], product_ids: List[int]) -> Optional[dict]:
    """
    Возвращаем структуру цен с блоком price: {price, old_price, marketing_price}.
    """
    url = "https://api-seller.ozon.ru/v5/product/info/prices"
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "cursor": "",
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
                log.error("prices %s: %s", resp.status, text)
                return None
            data = await resp.json()
    touch_alive("ozon_prices")
    return data

# -------------------- Команды --------------------
@dp.message(Command("start"))
async def start_cmd(message: Message):
    text = (
        "Привет! Я покажу цены для покупателя.\n\n"
        "Команды:\n"
        "• /prices — текущие цены\n"
        "• /monitor — что мониторим\n"
        "• /health — состояние бота\n"
    )
    await message.answer(text)
    touch_alive("cmd_start")

@dp.message(Command("prices"))
async def prices_cmd(message: Message):
    # 1) список товаров
    products = await get_ozon_products()
    if not products:
        await message.answer("Не удалось получить список товаров с Ozon.")
        return

    # оставим только нужные из .env (если заданы)
    products = filter_to_monitored(products)

    # 2) цены
    offer_ids = [it.get("offer_id") for it in products if it.get("offer_id")]
    product_ids = [it.get("product_id") for it in products if it.get("product_id")]

    prices = await get_ozon_prices(offer_ids, product_ids)
    if not prices:
        await message.answer("Ошибка при запросе цен к Ozon API.")
        return

    # 3) сопоставим, чтобы доставать название
    # product.list не отдаёт name, поэтому используем offer_id как «название»
    # (если у вас есть маппинг offer_id -> title, можно подложить его здесь)
    price_items = prices.get("items", []) or []
    by_key = {(str(i.get("offer_id")), int(i.get("product_id") or 0)): i for i in price_items}

    lines = ["Текущие цены для покупателя:"]
    for it in products:
        offer = str(it.get("offer_id"))
        pid = int(it.get("product_id") or 0)
        src = by_key.get((offer, pid))
        if not src:
            continue
        price, old_price, pct = pick_buyer_price(src)
        title = offer  # если нужно красивее — замените на свой словарь названий
        lines.append(format_price_line(title, pid, price, old_price, pct))

    for block in chunks(lines):
        await message.answer(block)
    touch_alive("cmd_prices")

@dp.message(Command("monitor"))
async def monitor_cmd(message: Message):
    if not MONITOR_OFFER_IDS and not MONITOR_PRODUCT_IDS:
        await message.answer("В .env не задано MONITOR_OFFER_IDS или MONITOR_PRODUCT_IDS — мониторю все товары.")
        return
    parts = ["Мониторю только следующие юниты:"]
    if MONITOR_OFFER_IDS:
        parts.append(f"• offer_id: {', '.join(MONITOR_OFFER_IDS)}")
    if MONITOR_PRODUCT_IDS:
        parts.append(f"• product_id: {', '.join(map(str, MONITOR_PRODUCT_IDS))}")
    await message.answer("\n".join(parts))

@dp.message(Command(commands=["health", "ping"]))
async def health_cmd(message: Message):
    now = datetime.now()
    hb, last_txt = human_health(now)
    uptime_min = int((now - STARTED_AT).total_seconds() // 60)

    if MONITOR_OFFER_IDS or MONITOR_PRODUCT_IDS:
        scope = []
        if MONITOR_OFFER_IDS:
            scope.append(f"offer_id ({len(MONITOR_OFFER_IDS)})")
        if MONITOR_PRODUCT_IDS:
            scope.append(f"product_id ({len(MONITOR_PRODUCT_IDS)})")
        scope_txt = ", ".join(scope)
    else:
        scope_txt = "все товары"

    text = (
        "Состояние бота\n"
        f"• Пульс: {hb} (порог {HEARTBEAT_MINUTES} мин)\n"
        f"• Последний успешный цикл: {last_txt}\n"
        f"• Аптайм: {uptime_min} мин\n"
        f"• Мониторинг: {scope_txt}\n"
        f"• Админы: {', '.join(map(str, ADMIN_IDS)) or '—'}"
    )
    await message.answer(text)
    touch_alive("cmd_health")

# -------------------- Периодическая проверка цен --------------------
async def check_prices_periodically():
    global last_cycle_at, previous_prices
    while True:
        try:
            products = await get_ozon_products()
            if not products:
                await asyncio.sleep(60)
                continue

            products = filter_to_monitored(products)

            offer_ids = [it.get("offer_id") for it in products if it.get("offer_id")]
            product_ids = [it.get("product_id") for it in products if it.get("product_id")]

            prices = await get_ozon_prices(offer_ids, product_ids)
            if not prices:
                await asyncio.sleep(60)
                continue

            for it in prices.get("items", []) or []:
                offer = str(it.get("offer_id") or "")
                price, _, _ = pick_buyer_price(it)
                old = previous_prices.get(offer)
                if old is not None and old != price:
                    diff = "↑" if price > old else "↓"
                    txt = f"Цена изменилась по {offer}: {old} → {price} ₽ {diff}"
                    for admin in ADMIN_IDS:
                        try:
                            await bot.send_message(admin, txt)
                        except Exception as e:
                            log.warning("send admin %s failed: %s", admin, e)
                previous_prices[offer] = price

            last_cycle_at = datetime.now()
            touch_alive("cycle_ok")
        except Exception as e:
            log.exception("periodic error: %s", e)

        await asyncio.sleep(300)  # каждые 5 минут

# -------------------- Heartbeat-монитор --------------------
async def heartbeat_watcher():
    if not HEARTBEAT_CHAT_ID:
        log.warning("HEARTBEAT_CHAT_ID not set — heartbeat-тихий режим.")
        return
    threshold = timedelta(minutes=HEARTBEAT_MINUTES)
    while True:
        try:
            if datetime.now() - last_activity > threshold:
                try:
                    mins = int((datetime.now() - last_activity).total_seconds() // 60)
                    await bot.send_message(
                        HEARTBEAT_CHAT_ID,
                        f"⚠️ Тишина: нет активности {mins} мин (порог {HEARTBEAT_MINUTES})."
                    )
                except Exception as e:
                    log.warning("send heartbeat alert failed: %s", e)
                touch_alive("heartbeat_alert")
        except Exception as e:
            log.error("heartbeat error: %s", e)
        await asyncio.sleep(60)

# -------------------- Обработка прочих сообщений --------------------
@dp.message()
async def any_message(msg: Message):
    touch_alive("incoming")
    # Неброское приветствие по любому слову (без эха команд):
    if not (isinstance(msg.text, str) and msg.text.strip().startswith("/")):
        await msg.answer("Привет! Команды: /start, /prices, /monitor, /health")

# -------------------- ENTRYPOINT --------------------
async def main():
    asyncio.create_task(check_prices_periodically())
    asyncio.create_task(heartbeat_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
