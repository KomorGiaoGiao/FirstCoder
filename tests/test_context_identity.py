from datetime import UTC, datetime
from enum import Enum

from firstcoder.context.identity import (
    content_fingerprint,
    new_event_id,
    new_message_id,
    new_part_id,
    new_session_id,
    stable_json_hash,
)


class ExampleMode(Enum):
    DEFAULT = "default"


def test_stable_json_hash_ignores_dict_key_order() -> None:
    left = {"role": "user", "metadata": {"b": 2, "a": 1}}
    right = {"metadata": {"a": 1, "b": 2}, "role": "user"}

    assert stable_json_hash(left) == stable_json_hash(right)


def test_stable_json_hash_accepts_common_config_objects() -> None:
    value = {
        "mode": ExampleMode.DEFAULT,
        "created_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    }

    assert stable_json_hash(value) == stable_json_hash(value)


def test_content_fingerprint_is_stable_and_short() -> None:
    first = content_fingerprint("hello\nworld")
    second = content_fingerprint("hello\nworld")

    assert first == second
    assert len(first) == 16


def test_generated_ids_have_readable_prefixes() -> None:
    assert new_session_id().startswith("sess_")
    assert new_message_id().startswith("msg_")
    assert new_part_id().startswith("part_")
    assert new_event_id().startswith("evt_")
