from backend.models import ChatMessage, ChatRequest, MessageRole


def test_chat_request_defaults_to_human_message():
    req = ChatRequest(message="hello")
    assert req.auto_continue is False
    assert req.auto_depth == 0


def test_chat_request_accepts_auto_continue_flags():
    req = ChatRequest(message="keep going", auto_continue=True, auto_depth=2)
    assert req.auto_continue is True
    assert req.auto_depth == 2


def test_chat_message_carries_auto_continue_metadata():
    msg = ChatMessage(
        role=MessageRole.USER,
        content="Continue automatically after entering RESEARCH.",
        metadata={"auto_continue": True, "auto_depth": 1},
    )
    assert msg.metadata["auto_continue"] is True
    assert msg.metadata["auto_depth"] == 1
    # Round-trip via JSON should keep the marker
    restored = ChatMessage.model_validate(msg.model_dump())
    assert restored.metadata == {"auto_continue": True, "auto_depth": 1}
