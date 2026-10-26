"""Native tool protocol integration in the configured ModelClient."""
from core.models.client import ModelClient


class FakeResponse:
    status_code = 200
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "search_records", "arguments": "{}"},
                    }],
                },
            }]
        }


class FakeSession:
    def __init__(self):
        self.payload = None

    def post(self, _url, json, **_kwargs):
        self.payload = json
        return FakeResponse()


def test_chat_with_tools_uses_native_protocol_and_returns_call_state():
    session = FakeSession()
    client = ModelClient.__new__(ModelClient)
    client._session = session
    client.llm_base_url = "http://model.local/v1"
    client.llm_model = "configured-model"
    client.llm_api_key = ""
    client.last_finish_reason = None
    tools = [{
        "type": "function",
        "function": {
            "name": "search_records",
            "description": "Search records.",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    result = client.chat_with_tools(
        [{"role": "user", "content": "Find a fact"}], tools)

    assert session.payload is not None
    assert session.payload["model"] == "configured-model"
    assert session.payload["tools"] == tools
    assert session.payload["tool_choice"] == "auto"
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0]["function"]["name"] == "search_records"
