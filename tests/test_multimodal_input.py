"""Multimodal user input: attachments, projection, and provider encoding."""

from __future__ import annotations

from pathlib import Path

from firstcoder.input import clipboard
from firstcoder.agent.session import AgentSession
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.store import JsonlSessionStore
from firstcoder.input.attachments import (
    attach_path,
    prepare_attachments_for_session,
    resolve_paste_attachments,
)
from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider
from firstcoder.providers.types import ChatMessage, ContentPart, ProviderCapabilities

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_session(tmp_path: Path) -> AgentSession:
    store = JsonlSessionStore(tmp_path)
    return AgentSession.create(store=store, session_id="sess_multi")


def test_resolve_paste_attachments_from_file_path(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(PNG_1x1)
    monkeypatch.setattr(
        "firstcoder.input.attachments.read_clipboard_image_bytes",
        lambda: None,
    )
    attachments = resolve_paste_attachments(str(image), include_clipboard_image=True)
    assert len(attachments) == 1
    assert attachments[0].kind == "image"
    assert attachments[0].filename == "shot.png"


def test_read_clipboard_image_bytes_uses_platform_reader(monkeypatch) -> None:
    monkeypatch.setattr(clipboard.platform, "system", lambda: "Linux")
    monkeypatch.setattr(clipboard, "_read_linux_clipboard_image", lambda: PNG_1x1)

    assert clipboard.read_clipboard_image_bytes() == PNG_1x1


def test_read_linux_clipboard_image_uses_wayland_then_xclip(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Completed:
        def __init__(self, returncode: int, stdout: bytes) -> None:
            self.returncode = returncode
            self.stdout = stdout

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "wl-paste":
            return Completed(1, b"")
        return Completed(0, PNG_1x1)

    monkeypatch.setattr(clipboard.subprocess, "run", run)

    assert clipboard._read_linux_clipboard_image() == PNG_1x1
    assert [command[0] for command in calls] == ["wl-paste", "xclip"]


def test_prepare_and_append_image_attachment(tmp_path: Path) -> None:
    image = tmp_path / "a.png"
    image.write_bytes(PNG_1x1)
    session = _make_session(tmp_path / "store")
    attachment = attach_path(image)
    message_id = session.append_user_message("look at this", attachments=[attachment])
    view = session.rebuild_view()
    message = next(item for item in view.messages if item.id == message_id)
    kinds = [part.kind for part in message.parts]
    assert "text" in kinds
    assert "image" in kinds
    image_part = next(part for part in message.parts if part.kind == "image")
    assert image_part.metadata["filename"] == "a.png"
    stored = session.store.root / image_part.metadata["path"]
    assert stored.is_file()
    assert stored.read_bytes() == PNG_1x1


def test_context_builder_projects_image_content_parts(tmp_path: Path) -> None:
    image = tmp_path / "b.png"
    image.write_bytes(PNG_1x1)
    store_root = tmp_path / "store"
    prepared = prepare_attachments_for_session(
        [attach_path(image)],
        store_root=store_root,
        session_id="sess_x",
    )[0]
    view = SessionView(
        session_id="sess_x",
        messages=[
            AgentMessage(
                id="msg_user",
                session_id="sess_x",
                role="user",
                parts=[
                    MessagePart(id="p1", message_id="msg_user", kind="text", content="describe"),
                    MessagePart(
                        id="p2",
                        message_id="msg_user",
                        kind="image",
                        content="[image: b.png]",
                        metadata={
                            "filename": prepared.filename,
                            "media_type": prepared.media_type,
                            "path": prepared.relative_path,
                            "bytes": prepared.size_bytes,
                        },
                    ),
                ],
            )
        ],
    )
    messages = ContextBuilder().build_provider_messages(view, store_root=store_root)
    assert len(messages) == 1
    message = messages[0]
    assert message.content_parts is not None
    assert message.content_parts[0].type == "text"
    assert "describe" in message.content_parts[0].text
    image_part = next(part for part in message.content_parts if part.type == "image")
    assert image_part.media_type == "image/png"
    assert image_part.data_base64


def test_context_builder_rejects_tampered_absolute_attachment_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_1x1)
    view = SessionView(
        session_id="sess_x",
        messages=[
            AgentMessage(
                id="msg_user",
                session_id="sess_x",
                role="user",
                parts=[
                    MessagePart(
                        id="p1",
                        message_id="msg_user",
                        kind="image",
                        content="[image: outside.png]",
                        metadata={
                            "filename": "outside.png",
                            "media_type": "image/png",
                            "absolute_path": str(outside),
                        },
                    )
                ],
            )
        ],
    )

    assert ContextBuilder().build_provider_messages(view, store_root=tmp_path) == []


def test_openai_and_anthropic_encode_multimodal_content() -> None:
    message = ChatMessage(
        role="user",
        content="hi",
        content_parts=[
            ContentPart(type="text", text="hi"),
            ContentPart(type="image", media_type="image/png", data_base64="abc123", filename="x.png"),
        ],
    )
    openai_payload = OpenAICompatibleProvider._to_openai_message(message)
    assert isinstance(openai_payload["content"], list)
    assert openai_payload["content"][0]["type"] == "text"
    assert openai_payload["content"][1]["type"] == "image_url"
    assert openai_payload["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    anthropic = AnthropicProvider(
        api_key="test",
        model="claude-test",
        capabilities=ProviderCapabilities(supports_streaming=True, supports_vision=True),
        client=object(),
    )
    anthropic_messages = anthropic._to_anthropic_messages([message])
    assert anthropic_messages[0]["role"] == "user"
    content = anthropic_messages[0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image"
    assert content[1]["source"]["data"] == "abc123"


def test_text_file_attachment_is_inlined(tmp_path: Path) -> None:
    source = tmp_path / "note.txt"
    source.write_text("hello attachment\n", encoding="utf-8")
    session = _make_session(tmp_path / "store")
    message_id = session.append_user_message("read this", attachments=[attach_path(source)])
    view = session.rebuild_view()
    message = next(item for item in view.messages if item.id == message_id)
    file_part = next(part for part in message.parts if part.kind == "file")
    assert "hello attachment" in file_part.content
    projected = ContextBuilder().build_provider_messages(view, store_root=session.store.root)
    assert "hello attachment" in projected[0].content
