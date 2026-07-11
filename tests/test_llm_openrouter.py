from app.agent.llm import _from_openai_response, _to_openai_messages, _to_openai_tools, llm_enabled


def test_openrouter_tool_schema_conversion():
    tools = [
        {
            "name": "search_admission",
            "description": "Find admission",
            "input_schema": {
                "type": "object",
                "properties": {"destination": {"type": "string"}},
                "required": ["destination"],
            },
        }
    ]

    converted = _to_openai_tools(tools)

    assert converted == [
        {
            "type": "function",
            "function": {
                "name": "search_admission",
                "description": "Find admission",
                "parameters": {
                    "type": "object",
                    "properties": {"destination": {"type": "string"}},
                    "required": ["destination"],
                },
            },
        }
    ]


def test_openrouter_message_conversion_keeps_tool_round_trip():
    messages = [
        {"role": "user", "content": "хочу поступление"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "search_admission",
                    "input": {"destination": "Программная инженерия"},
                }
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "Hotel X"}]},
    ]

    converted = _to_openai_messages(messages)

    assert converted[1]["role"] == "assistant"
    assert converted[1]["tool_calls"][0]["function"]["name"] == "search_admission"
    assert '"destination": "Программная инженерия"' in converted[1]["tool_calls"][0]["function"]["arguments"]
    assert converted[2] == {"role": "tool", "tool_call_id": "call_1", "content": "Hotel X"}


def test_openrouter_response_conversion_to_agent_shape():
    resp = _from_openai_response(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "score_admission",
                                    "arguments": '{"country": "9"}',
                                },
                            }
                        ]
                    }
                }
            ]
        }
    )

    assert resp.stop_reason == "tool_use"
    assert resp.content[0].type == "tool_use"
    assert resp.content[0].name == "score_admission"
    assert resp.content[0].input == {"country": "9"}


def test_llm_enabled_uses_openrouter_key_only(monkeypatch):
    from app.agent import llm

    monkeypatch.setattr(llm.settings, "openrouter_api_key", "")
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "old-key")
    assert llm_enabled() is False

    monkeypatch.setattr(llm.settings, "openrouter_api_key", "or-key")
    assert llm_enabled() is True

