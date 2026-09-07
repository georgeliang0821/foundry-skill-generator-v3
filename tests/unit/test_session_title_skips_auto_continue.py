from backend.models import ChatMessage, MessageRole, Session
from backend.session_store import LocalSessionStore


def test_session_title_skips_auto_continue_user_messages(tmp_path):
    session = Session()
    session.conversation = [
        ChatMessage(
            role=MessageRole.USER,
            content=(
                "Continue automatically after entering RESEARCH. "
                "Start the next stage action now."
            ),
            metadata={"auto_continue": True, "auto_depth": 1},
        ),
        ChatMessage(
            role=MessageRole.USER,
            content="Build a calendar skill that lists events.",
            metadata={},
        ),
    ]
    store = LocalSessionStore(root=tmp_path)
    summary = store.summary(session)
    assert summary.title.startswith("Build a calendar skill")
    assert "Continue automatically" not in summary.title


def test_session_title_falls_back_to_untitled_when_only_auto_continue(tmp_path):
    session = Session()
    session.conversation = [
        ChatMessage(
            role=MessageRole.USER,
            content="Continue automatically after entering RESEARCH.",
            metadata={"auto_continue": True},
        )
    ]
    store = LocalSessionStore(root=tmp_path)
    summary = store.summary(session)
    assert summary.title == "Untitled session"
