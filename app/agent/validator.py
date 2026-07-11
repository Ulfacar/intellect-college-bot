"""Outgoing reply validator for the college bot.

Only formatting is auto-fixed. Risky facts are logged by returning violation codes.
"""
from __future__ import annotations

import re

MAX_LEN = 600

_BOLD = re.compile(r"\*{1,3}(.+?)\*{1,3}", re.DOTALL)
_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s{0,3}[-*•]\s+", re.MULTILINE)
_MULTINL = re.compile(r"\n{3,}")
_SPACED_DASH = re.compile(r"\s+[—–]\s+")

_PRICE = re.compile(
    r"(?:\$\s?\d[\d\s.,]*|\d[\d\s.,]*\s?(?:\$|usd|долл|сом|руб|eur|€|тыс))",
    re.IGNORECASE,
)
_ADMISSION_GUARANTEE = re.compile(
    r"(100\s?%|(?<!не )гаранти\w*|кепилдик\w*"
    r"|точно (?:поступ|зачисл|пройд[её]те|получите грант)"
    r"|обязательно (?:поступ|зачисл|пройд[её]те)"
    r"|(?:грант|скидк\w+|зачислени\w+) (?:обеспечен|гарантирован)\w*)",
    re.IGNORECASE,
)
_DISCOUNT_AMOUNT = re.compile(
    r"(скидк\w*|арзандат\w*)[^.\n]{0,40}?\d+\s?%|"
    r"\d+\s?%[^.\n]{0,25}(скидк|арзандат)",
    re.IGNORECASE,
)
_PASSING_SCORE = re.compile(
    r"(проходн\w+ балл|өтүү балл\w*)[^.\n]{0,30}\d+|"
    r"\d+\s?балл\w*[^.\n]{0,30}(проходн|өтүү|порог)",
    re.IGNORECASE,
)
_DURATION_3Y = re.compile(r"\b(3|три|үч)\s*(год|года|жыл)", re.IGNORECASE)


def strip_markdown(text: str) -> str:
    text = _BOLD.sub(r"\1", text)
    text = _UNDERSCORE.sub(r"\1", text)
    text = _HEADER.sub("", text)
    text = _BULLET.sub("", text)
    text = _MULTINL.sub("\n\n", text)
    text = _SPACED_DASH.sub(". ", text)
    return text.strip()


def _price_numbers(text: str) -> list[int]:
    numbers: list[int] = []
    for match in _PRICE.finditer(text):
        raw = re.sub(r"[^\d]", "", match.group(0))
        if raw:
            numbers.append(int(raw))
    return numbers


def validate_reply(text: str, funnel: str | None) -> tuple[str, list[str]]:
    violations: list[str] = []
    clean = strip_markdown(text)
    if clean != text.strip():
        violations.append("markdown")

    if funnel == "admission":
        if _ADMISSION_GUARANTEE.search(clean):
            violations.append("admission_guarantee")
        if any(n >= 100 and n != 6500 for n in _price_numbers(clean)):
            violations.append("admission_price_mismatch")
        if _DISCOUNT_AMOUNT.search(clean):
            violations.append("admission_discount_amount")
        if _PASSING_SCORE.search(clean):
            violations.append("admission_passing_score")
        if _DURATION_3Y.search(clean):
            violations.append("admission_duration_claim")

    if len(clean) > MAX_LEN:
        violations.append("too_long")
    if clean.count("?") > 1:
        violations.append("multiple_questions")

    return clean, violations
