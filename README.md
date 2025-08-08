# Meteorite Bot (Telegram + Ozon Seller API)

## Быстрый старт
1) Создай `.env` рядом с `bot.py` по образцу `.env.example`.
2) Установи зависимости и запусти:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```
3) Команды в Telegram:
- `/start` — приветствие
- `/ping` — проверка, что бот жив
- `/prices` — получить цены первых 10 товаров из Ozon

## Настройка через systemd (на сервере)
Сервис уже показывали в чате, повторю кратко:
```ini
[Service]
WorkingDirectory=/opt/meteorite_bot
EnvironmentFile=/opt/meteorite_bot/.env
ExecStart=/opt/meteorite_bot/.venv/bin/python /opt/meteorite_bot/bot.py
Restart=always
User=ubuntu
Group=ubuntu
```
