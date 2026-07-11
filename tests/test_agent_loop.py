import asyncio
from unittest.mock import AsyncMock

from app.agent import runner
from app.core.state import DialogState
from app.integrations.crm import get_crm


class FakeBlock:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type = type
        self.text = text
        self.name = name
        self.input = input or {}
        self.id = id

    def model_dump(self):
        return {"type": self.type, "text": self.text, "name": self.name, "input": self.input, "id": self.id}


class FakeResp:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


def _tool_use(name="ask_qualification", inp=None, id="t1"):
    return FakeResp("tool_use", [FakeBlock("tool_use", name=name, input=inp or {}, id=id)])


def _text(text="Готово"):
    return FakeResp("end_turn", [FakeBlock("text", text=text)])


def _patch_client(monkeypatch, *responses, repeat_last=False):
    fake = AsyncMock()
    fake.messages.create = AsyncMock(return_value=responses[-1] if repeat_last else None)
    if not repeat_last:
        fake.messages.create = AsyncMock(side_effect=list(responses))
    monkeypatch.setattr(runner, "client", lambda: fake)
    return fake


def test_tool_use_then_text(monkeypatch):
    state = DialogState(user_id="u1", funnel="admission")
    fake = _patch_client(
        monkeypatch,
        _tool_use(inp={"field": "name", "question": "Как вас зовут?", "name": "Айбек"}),
        _text("Айбек, подскажите, после 9 или 11 класса?"),
    )

    reply = asyncio.run(runner.run_admission_turn(state, "я Айбек"))

    assert reply == "Айбек, подскажите, после 9 или 11 класса?"
    assert state.deal_id is not None
    assert state.qualification["name"] == "Айбек"
    assert fake.messages.create.await_count == 2


def test_admission_turn_uses_bot_specific_manager_name(monkeypatch):
    state = DialogState(user_id="u-aidana", funnel="admission", manager_name="Мээрим")
    fake = _patch_client(monkeypatch, _text("Ок"))

    reply = asyncio.run(runner.run_admission_turn(state, "здравствуйте"))

    assert reply == "Ок"
    system = fake.messages.create.await_args.kwargs["system"]
    assert "Мээрим" in system
    assert "Айдана" not in system


def test_max_iterations_guard(monkeypatch):
    state = DialogState(user_id="u2", funnel="admission")
    fake = _patch_client(monkeypatch, _tool_use(inp={"field": "grade_base", "question": "9 или 11?"}), repeat_last=True)

    reply = asyncio.run(runner.run_admission_turn(state, "привет"))

    assert reply
    assert fake.messages.create.await_count == runner.MAX_TOOL_ITERATIONS


def test_admission_hot_lead_asks_name_before_test_invite():
    state = DialogState(user_id="u-test-name", funnel="admission")
    crm = get_crm()

    out = asyncio.run(runner._admission_exec_tool(
        "escalate_to_office",
        {"reason": "клиент хочет на тест"},
        state,
        crm,
    ))

    assert state.stage == "greeting"
    assert "имя не собрано" in out.lower()
    assert "НЕ подтверждай запись" in out


def test_admission_test_invite_after_name():
    state = DialogState(user_id="u-test-ok", funnel="admission")
    crm = get_crm()

    out = asyncio.run(runner._admission_exec_tool(
        "escalate_to_office",
        {"reason": "клиент хочет на тест", "name": "Айбек", "grade_base": "9"},
        state,
        crm,
    ))

    assert state.stage == "test_invite"
    assert state.qualification["name"] == "Айбек"
    assert "проходной балл" in out
    assert "1,5 часа" in out


def test_handoff_stores_reason_and_stage():
    state = DialogState(user_id="u-handoff", funnel="admission", qualification={"name": "Айбек"})
    crm = get_crm()

    out = asyncio.run(runner._admission_exec_tool(
        "handoff_to_manager",
        {"reason": "вопрос вне базы"},
        state,
        crm,
    ))

    assert state.stage == "manager"
    assert state.qualification["escalation_reason"] == "вопрос вне базы"
    assert "НЕ отвечай сам" in out


def test_crm_update_stage_allows_only_bot_stages():
    state = DialogState(user_id="u-stage", funnel="admission")
    crm = get_crm()

    ok = asyncio.run(runner._admission_exec_tool("crm_update_stage", {"stage": "consulting"}, state, crm))
    bad = asyncio.run(runner._admission_exec_tool("crm_update_stage", {"stage": "won"}, state, crm))

    assert state.stage == "consulting"
    assert "Стадия обновлена" in ok
    assert "Недопустимая" in bad

