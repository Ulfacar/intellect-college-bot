"""Тесты бэкендов хранилища состояний (in-memory и Redis).

RedisStateStore проверяем на фейковом клиенте (dict с TTL) — без реального Redis:
интересует сериализация DialogState ↔ JSON и контракт load/save, не сетевой слой.
"""
import asyncio

from app.core.state import DialogState, RedisStateStore, StateStore, get_state_store


class FakeRedis:
    """Минимальный async-двойник redis.asyncio: get/set с запоминанием ex (TTL)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value
        self.ttls[key] = ex


def test_dialog_state_json_round_trip():
    """to_json/from_json сохраняют все поля, включая историю и флаг перехвата."""
    state = DialogState(
        user_id="u1", funnel="visa", stage="scoring",
        qualification={"country": "Германия", "prior_visas": "да"},
        deal_id="42", history=[{"role": "user", "content": "привет"}],
        intercepted=True,
    )
    restored = DialogState.from_json(state.to_json())
    assert restored == state


def test_redis_store_save_then_load():
    """save сериализует в Redis, load восстанавливает тот же стейт."""
    fake = FakeRedis()
    store = RedisStateStore(redis_client=fake, ttl=123)
    state = DialogState(user_id="u2", funnel="tours")
    state.qualification["destination"] = "Турция"

    asyncio.run(store.save(state))
    loaded = asyncio.run(store.load("u2"))

    assert loaded.qualification["destination"] == "Турция"
    assert fake.data["frunze:dialog:u2"]  # лежит под ожидаемым ключом
    assert fake.ttls["frunze:dialog:u2"] == 123  # TTL проставлен


def test_redis_store_load_missing_returns_fresh():
    """Неизвестный user → новый пустой DialogState, а не ошибка."""
    store = RedisStateStore(redis_client=FakeRedis())
    loaded = asyncio.run(store.load("never-seen"))
    assert loaded.user_id == "never-seen"
    assert loaded.funnel is None


def test_get_state_store_defaults_to_memory(monkeypatch):
    """По умолчанию (memory) фабрика отдаёт общий in-memory singleton."""
    from app.core import state as state_mod

    monkeypatch.setattr(state_mod.settings, "state_backend", "memory")
    assert isinstance(get_state_store(), StateStore)
