import asyncio
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from config import settings
from ozon import OzonClient

router = Router()

@router.message(CommandStart())
async def start(msg: types.Message):
    await msg.answer("Привет! Я Meteorite Bot. Команды: /ping, /prices")

@router.message(Command("ping"))
async def ping(msg: types.Message):
    await msg.answer("pong ✅")

@router.message(Command("prices"))
async def prices(msg: types.Message):
    if not settings.ozon_client_id or not settings.ozon_api_key:
        await msg.answer("Ozon API не настроен. Добавьте OZON_CLIENT_ID и OZON_API_KEY в .env")
        return
    client = OzonClient(settings.ozon_client_id, settings.ozon_api_key)
    try:
        products = client.list_products()
        ids = [int(it["product_id"]) for it in products[:10]]
        price_map = client.prices_by_product_ids(ids)
    except Exception as e:
        await msg.answer(f"Ошибка Ozon API: {e}")
        return

    lines = []
    for offer_id, p in list(price_map.items())[:10]:
        lines.append(f"{offer_id}: {p.get('price')} {p.get('currency_code')} (old: {p.get('old_price')}, disc: {p.get('price_with_discount')})")
    text = "Цены (первые 10):\n" + "\n".join(lines) if lines else "Нет данных."
    await msg.answer(text)

async def main():
    if not settings.tg_token:
        raise SystemExit("Не задан TG_TOKEN в .env")
    bot = Bot(settings.tg_token, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
