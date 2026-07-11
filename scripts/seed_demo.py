#!/usr/bin/env python3
"""Наполнить админ-панель демо-диалогами admission-сценария.

Запуск внутри контейнера app:
    python scripts/seed_demo.py

Удалить демо-данные:
    python scripts/seed_demo.py --clear
"""
import asyncio
import sys

from sqlalchemy import text

from app.integrations.crm.db import get_sessionmaker, init_db
from app.integrations.panel.store import PostgresConversationStore

DEMO_PREFIX = "996555000"

# (bot_id, phone, stage, temp, assigned, intercepted, outcome, summary, next_step, qual, msgs)
D = [
    (
        "college_1", "996555000101", "greeting", "new", "", False, "", "", "",
        {},
        [
            ("client", "Здравствуйте, хочу поступить в колледж"),
            ("bot", "Здравствуйте! Подскажите, вы рассматриваете поступление после 9 или после 11 класса?"),
        ],
    ),
    (
        "college_1", "996555000102", "qualification", "new", "", False, "",
        "Абитуриент после 9 класса, интересуется IT.", "Уточнить имя и направление.",
        {"grade_base": "9", "direction": "IT"},
        [
            ("client", "Я после 9, интересует IT"),
            ("bot", "Как я могу к вам обращаться?"),
        ],
    ),
    (
        "college_2", "996555000201", "manager", "warm", "manager", True, "manager",
        "Вопрос про оплату и договор, нужна консультация менеджера.",
        "Ответить по оплате и договору.",
        {"name": "Алия", "grade_base": "11"},
        [
            ("client", "Меня зовут Алия, после 11. Можно оплатить в рассрочку?"),
            ("bot", "Передала вопрос менеджеру приёмной комиссии — он ответит здесь."),
            ("manager", "Алия, здравствуйте! Сейчас расскажу варианты оплаты."),
        ],
    ),
    (
        "college_3", "996555000301", "office", "hot", "", False, "office",
        "Готов записаться на вступительный тест.",
        "Подтвердить дату, время и формат теста.",
        {"name": "Айбек", "grade_base": "9", "direction": "Программная инженерия и ИИ"},
        [
            ("client", "Я Айбек, после 9, хочу на программную инженерию и записаться на тест"),
            ("bot", "Спасибо, Айбек! Передала заявку на вступительный тест менеджеру — он подтвердит дату, время и формат."),
        ],
    ),
]


async def clear(sm):
    async with sm() as s:
        await s.execute(
            text(
                "DELETE FROM messages WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE phone LIKE :p)"
            ),
            {"p": DEMO_PREFIX + "%"},
        )
        await s.execute(text("DELETE FROM conversations WHERE phone LIKE :p"), {"p": DEMO_PREFIX + "%"})
        await s.commit()


async def main(clear_only: bool):
    await init_db()
    sm = get_sessionmaker()
    store = PostgresConversationStore(sessionmaker=sm)
    await clear(sm)
    if clear_only:
        print("Демо-диалоги удалены.")
        return
    for (bot_id, phone, stage, temp, assigned, intercepted, outcome, summary, nxt, qual, msgs) in D:
        key = f"{bot_id}:{phone}"
        for sender, txt in msgs:
            status = "delivered" if sender in {"bot", "manager"} else ""
            await store.add_message(
                key, sender, txt, channel="whatsapp", bot_id=bot_id,
                chat_id=phone + "@c.us", phone=phone, status=status,
            )
        await store.update_meta(
            key, funnel="admission", stage=stage, qualification=qual,
            lead_temperature=temp, assigned_to=assigned, intercepted=intercepted,
            outcome=outcome, ai_summary=summary, manager_next_step=nxt,
        )
    print(f"Создано демо-диалогов: {len(D)}")


if __name__ == "__main__":
    asyncio.run(main("--clear" in sys.argv))
