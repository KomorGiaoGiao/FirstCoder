from firstcoder.context.checkpoint import Checkpoint, CheckpointIndex
from firstcoder.context.versions import CHECKPOINT_STRATEGY_VERSION


def test_latest_checkpoint_is_selected() -> None:
    older = Checkpoint(
        id="ckpt_old",
        session_id="sess_test",
        summary="旧摘要",
        tail_start_message_id="msg_2",
        covered_until_message_id="msg_1",
        source_fingerprint="source_old",
        created_at="2026-06-01T00:00:01Z",
    )
    newer = Checkpoint(
        id="ckpt_new",
        session_id="sess_test",
        summary="新摘要",
        tail_start_message_id="msg_4",
        covered_until_message_id="msg_3",
        source_fingerprint="source_new",
        created_at="2026-06-01T00:00:02Z",
    )

    assert CheckpointIndex([older, newer]).latest() == newer


def test_tail_start_message_id_moves_monotonically() -> None:
    index = CheckpointIndex(
        [
            Checkpoint(
                id="ckpt_older",
                session_id="sess_test",
                summary="旧 checkpoint",
                tail_start_message_id="msg_random_c",
                covered_until_message_id="msg_random_b",
                source_fingerprint="source_older",
                created_at="2026-06-01T00:00:02Z",
            ),
            Checkpoint(
                id="ckpt_newer",
                session_id="sess_test",
                summary="新 checkpoint",
                tail_start_message_id="msg_random_a",
                covered_until_message_id="msg_random_z",
                source_fingerprint="source_newer",
                created_at="2026-06-01T00:00:03Z",
            ),
        ]
    )

    assert index.latest().id == "ckpt_newer"


def test_latest_checkpoint_uses_sequence_when_created_at_ties() -> None:
    index = CheckpointIndex(
        [
            Checkpoint(
                id="ckpt_random_z",
                session_id="sess_test",
                summary="先写入的 checkpoint",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
                source_fingerprint="source_older",
                created_at="2026-06-01T00:00:00Z",
                sequence=1,
            ),
            Checkpoint(
                id="ckpt_random_a",
                session_id="sess_test",
                summary="后写入的 checkpoint",
                tail_start_message_id="msg_3",
                covered_until_message_id="msg_2",
                source_fingerprint="source_newer",
                created_at="2026-06-01T00:00:00Z",
                sequence=2,
            ),
        ]
    )

    assert index.latest().id == "ckpt_random_a"


def test_checkpoint_round_trip_dict_keeps_strategy_version() -> None:
    checkpoint = Checkpoint(
        id="ckpt_1",
        session_id="sess_test",
        summary="摘要",
        tail_start_message_id="msg_2",
        covered_until_message_id="msg_1",
        source_fingerprint="source_1",
        created_at="2026-06-01T00:00:00Z",
    )

    restored = Checkpoint.from_dict(checkpoint.to_dict())

    assert restored == checkpoint
    assert restored.strategy_version == CHECKPOINT_STRATEGY_VERSION
