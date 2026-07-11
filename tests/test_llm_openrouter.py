from app.agent.llm import _from_openai_response, _to_openai_messages, _to_openai_tools, llm_enabled


def test_openrouter_tool_schema_conversion():
    tools = [
        {
            "name": "search_tours",
            "description": "Find tours",
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
                "name": "search_tours",
                "description": "Find tours",
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
        {"role": "user", "content": "хочу тур"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "search_tours",
                    "input": {"destination": "Турция"},
                }
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "Hotel X"}]},
    ]

    converted = _to_openai_messages(messages)

    assert converted[1]["role"] == "assistant"
    assert converted[1]["tool_calls"][0]["function"]["name"] == "search_tours"
    assert '"destination": "Турция"' in converted[1]["tool_calls"][0]["function"]["arguments"]
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
                                    "name": "score_visa",
                                    "arguments": '{"country": "Германия"}',
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
    assert resp.content[0].name == "score_visa"
    assert resp.content[0].input == {"country": "Германия"}


def test_llm_enabled_uses_openrouter_key_only(monkeypatch):
    from app.agent import llm

    monkeypatch.setattr(llm.settings, "openrouter_api_key", "")
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "old-key")
    assert llm_enabled() is False

    monkeypatch.setattr(llm.settings, "openrouter_api_key", "or-key")
    assert llm_enabled() is True
