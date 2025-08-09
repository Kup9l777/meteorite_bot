import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv
import aiohttp

load_dotenv()

logging.basicConfig(level=logging.INFO)

TG_TOKEN = os.getenv('TG_TOKEN')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(',')))
OZON_CLIENT_ID = os.getenv('OZON_CLIENT_ID')
OZON_API_KEY = os.getenv('OZON_API_KEY')

bot = Bot(token=TG_TOKEN, default_parse_mode=ParseMode.HTML)
dp = Dispatcher()

# Словарь для хранения последних известных цен
previous_prices = {}

async def get_ozon_products(limit=100):
    url = "https://api-seller.ozon.ru/v3/product/list"
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json"
    }
    offset = 0
    all_items = []

    async with aiohttp.ClientSession() as session:
        while True:
            payload = {
                "filter": {"visibility": "ALL"},
                "limit": limit,
                "offset": offset
            }
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logging.error(f"Failed to fetch products: {resp.status} - {text}")
                    break
                data = await resp.json()
                items = data.get("result", {}).get("items", [])
                if not items:
                    break
                all_items.extend(items)
                offset += len(items)
                if len(items) < limit:
                    break
    return all_items

async def get_ozon_prices(offer_ids, product_ids):
    url = "https://api-seller.ozon.ru/v5/product/info/prices"
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "cursor": "",
        "filter": {
            "offer_id": offer_ids,
            "product_id": product_ids,
            "visibility": "ALL"
        },
        "limit": 100
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                logging.error(f"Failed to fetch prices: {resp.status} - {text}")
                return None
            return await resp.json()

@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer("Привет! Я бот. Используй команду /prices для проверки цен.")

@dp.message(Command("prices"))
async def prices_handler(message: Message):
    products = await get_ozon_products()
    if not products:
        await message.answer("Не удалось получить список товаров с Ozon.")
        return

    offer_ids = [item.get("offer_id") for item in products if item.get("offer_id")]
    product_ids = [item.get("product_id") for item in products if item.get("product_id")]

    prices_result = await get_ozon_prices(offer_ids, product_ids)
    if prices_result is None:
        await message.answer("Ошибка при запросе цен к Ozon API.")
        return

    text_lines = []
    for item in prices_result.get("items", []):
        offer = item.get("offer_id")
        product = item.get("product_id")
        marketing_price = item.get("price", {}).get("marketing_price")
        regular_price = item.get("price", {}).get("price")
        old_price = item.get("price", {}).get("old_price")
        
        # Приоритет - marketing_price, если есть и > 0
        price = marketing_price if marketing_price and marketing_price > 0 else regular_price
        
        discount = old_price - price if old_price and price else 0

        line = f"<b>{offer}</b> (Product ID: {product}): Цена {price} руб."
        if discount > 0:
            line += f" (скидка {discount} руб.)"
        text_lines.append(line)

    if text_lines:
        max_len = 4000
        msg = ""
        for line in text_lines:
            if len(msg) + len(line) + 1 > max_len:
                await message.answer(msg)
                msg = ""
            msg += line + "\n"
        if msg:
            await message.answer(msg)
    else:
        await message.answer("Цены не найдены или товары отсутствуют.")

async def check_prices_periodically():
    while True:
        products = await get_ozon_products()
        offer_ids = [item.get("offer_id") for item in products if item.get("offer_id")]
        product_ids = [item.get("product_id") for item in products if item.get("product_id")]

        prices_result = await get_ozon_prices(offer_ids, product_ids)
        if prices_result is None:
            logging.error("Ошибка при запросе цен к Ozon API в периодической проверке.")
            await asyncio.sleep(300)
            continue

        for item in prices_result.get("items", []):
            offer = item.get("offer_id")
            marketing_price = item.get("price", {}).get("marketing_price")
            regular_price = item.get("price", {}).get("price")
            
            price = marketing_price if marketing_price and marketing_price > 0 else regular_price
            
            old_price = previous_prices.get(offer)
            if old_price is not None and old_price != price:
                text = f"Цена изменилась для <b>{offer}</b>: {old_price} → {price} руб."
                for admin_id in ADMIN_IDS:
                    await bot.send_message(admin_id, text)
            previous_prices[offer] = price

        await asyncio.sleep(300)  # Проверять каждые 5 минут

async def main():
    asyncio.create_task(check_prices_periodically())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
