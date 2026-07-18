"""STAGING 1 (owner follow-up) — инварианты deployment-конфигурации Caddy.

Директива `request_body { max_size }` в Caddyfile требует Caddy >= 2.10.0, поэтому
закреплённый образ обязан быть полным patch-тегом не ниже 2.10.0, БЕЗ плавающих тегов,
и compose/runbook/env-пример должны ссылаться на ОДИН и тот же тег.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.staging.yml"
CADDYFILE = ROOT / "Caddyfile"
RUNBOOK = ROOT / "docs" / "staging-1-runbook.md"
ENV_EXAMPLE = ROOT / ".env.staging.example"

_MIN_CADDY = (2, 10, 0)   # request_body { max_size } доступна начиная с Caddy 2.10.0
_DEPLOY_FILES = (COMPOSE, CADDYFILE, RUNBOOK, ENV_EXAMPLE)


def _compose_caddy_tag() -> str:
    m = re.search(r"image:\s*caddy:(\S+)", COMPOSE.read_text(encoding="utf-8"))
    assert m, "caddy image tag not found in docker-compose.staging.yml"
    return m.group(1)


def _ver(tag: str) -> tuple[int, int, int]:
    parts = tag.split(".")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def test_caddy_image_is_pinned_full_patch_tag():
    tag = _compose_caddy_tag()
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag), f"caddy tag must be full X.Y.Z, got {tag!r}"


def test_caddy_version_at_least_2_10_0():
    assert _ver(_compose_caddy_tag()) >= _MIN_CADDY, (
        f"caddy {_compose_caddy_tag()} < 2.10.0 несовместим с request_body"
    )


def test_request_body_forbidden_below_caddy_2_10():
    # Если Caddyfile использует request_body — закреплённый образ ДОЛЖЕН быть >= 2.10.0.
    caddy_text = CADDYFILE.read_text(encoding="utf-8")
    if "request_body" in caddy_text:
        assert _ver(_compose_caddy_tag()) >= (2, 10, 0)


def test_no_floating_or_incompatible_caddy_tags_anywhere():
    for path in _DEPLOY_FILES:
        text = path.read_text(encoding="utf-8")
        assert "caddy:latest" not in text, f"caddy:latest в {path.name}"
        assert "caddy:2.8" not in text, f"caddy:2.8 (несовместим с request_body) в {path.name}"
        assert "caddy:2.9" not in text, f"caddy:2.9 (несовместим с request_body) в {path.name}"
        # плавающий minor без patch: 'caddy:2.10' / 'caddy:2.11' не должно быть без .patch
        assert not re.search(r"caddy:2\.1\d(?!\.\d)", text), f"плавающий minor-тег caddy в {path.name}"


def test_compose_runbook_env_use_same_caddy_tag():
    tag = _compose_caddy_tag()
    for path in (RUNBOOK, ENV_EXAMPLE, CADDYFILE):
        assert f"caddy:{tag}" in path.read_text(encoding="utf-8"), (
            f"{path.name} должен ссылаться на тот же тег caddy:{tag}"
        )
