"""Валидатор исходящих реплик бота — «ремень безопасности» поверх промптов.

Проверяет сгенерированный LLM ответ ДО отправки клиенту:
- АВТО-ЧИНИТ безопасное: убирает markdown (мессенджер, не лендинг); для туров дописывает
  дисклеймер про изменчивость цен, если в ответе есть сумма, а оговорки нет.
- МЯГКО ЛОГИРУЕТ рискованное (гарантии визы, цены в билетах, длина, много вопросов) — БЕЗ
  правки текста, чтобы случайно не испортить корректный ответ (напр. «визу НЕ гарантируем»
  или эхо бюджета клиента «поняла, 500$»). Профилактика этого — в самих промптах.
  NB: цены в ВИЗОВОЙ воронке больше не «нарушение» — заказчик разрешил называть официальный
  прайс услуг (см. VISA_SERVICE_PRICES); ремень безопасности здесь — только детектор гарантий.

Чистые функции (тестируемо). Подключается в `app/agent/runner.run_turn`; сводка сработок —
через `observ.note_validation` (видна на /admin/system).
"""
from __future__ import annotations

import re

from app.core.branding import PRICE_DISCLAIMER

MAX_LEN = 600  # мягкий лимит длины реплики (символов) — только для лога, текст не режем

# --- markdown-разметка, неуместная в мессенджере ---
_BOLD = re.compile(r"\*{1,3}(.+?)\*{1,3}", re.DOTALL)       # **жирный** / *курсив*
_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)     # # Заголовок
_BULLET = re.compile(r"^\s{0,3}[-*•]\s+", re.MULTILINE)      # «- пункт» / «* пункт»
_MULTINL = re.compile(r"\n{3,}")
_SPACED_DASH = re.compile(r"\s+[—–]\s+")

# деньги: 250$, $300, 5000 сом, 185 usd, 5 тыс$
_PRICE = re.compile(
    r"(?:\$\s?\d[\d\s.,]*|\d[\d\s.,]*\s?(?:\$|usd|долл|сом|руб|eur|€|тыс))",
    re.IGNORECASE)
# утвердительная гарантия визы (для ЛОГА, не для правки). «не гарантируем» исключаем.
_GUARANTEE = re.compile(
    r"(100\s?%|(?<!не )гаранти\w*|обязательно (?:дад|одобр|получ)|"
    r"точно (?:дад|одобр|получ)|виза будет точно)",
    re.IGNORECASE)
# дисклеймер про цену уже присутствует?
_DISCLAIMER_MARK = re.compile(
    r"варьир|зависит от дат|может меня|уточним|подтвердим", re.IGNORECASE)


def strip_markdown(text: str) -> str:
    """Снять markdown-разметку → обычный текст мессенджера."""
    text = _BOLD.sub(r"\1", text)
    text = _UNDERSCORE.sub(r"\1", text)
    text = _HEADER.sub("", text)
    text = _BULLET.sub("", text)
    text = _MULTINL.sub("\n\n", text)
    text = _SPACED_DASH.sub(". ", text)
    return text.strip()


def validate_reply(text: str, funnel: str | None) -> tuple[str, list[str]]:
    """Вернуть (очищенный_текст, список_нарушений).

    Мутируем ТОЛЬКО безопасное (markdown, дисклеймер цен туров). Остальное — в список
    нарушений для логирования, текст не трогаем.
    """
    violations: list[str] = []

    clean = strip_markdown(text)
    if clean != text.strip():
        violations.append("markdown")

    has_price = bool(_PRICE.search(clean))

    # Туры: цены показываем, но обязателен дисклеймер изменчивости — допишем, если забыт.
    if funnel == "tours" and has_price and not _DISCLAIMER_MARK.search(clean):
        clean = f"{clean}\n\n{PRICE_DISCLAIMER}"
        violations.append("tours_price_disclaimer_added")

    # --- мягкие сигналы (только лог, без правки текста) ---
    # Визы теперь называют официальный прайс услуг → цена тут НЕ нарушение. Билеты — цену
    # называет менеджер, поэтому сумму в реплике бота помечаем.
    if funnel == "tickets" and has_price:
        violations.append("price_in_no_price_funnel")
    if funnel == "visa" and _GUARANTEE.search(clean):
        violations.append("possible_visa_guarantee")
    if len(clean) > MAX_LEN:
        violations.append("too_long")
    if clean.count("?") > 1:
        violations.append("multiple_questions")

    return clean, violations
