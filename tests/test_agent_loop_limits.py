from firstcoder.agent.loop_limits import AgentLoopLimits, AgentLoopStopReason


def test_default_limits_match_tui_goal_profile() -> None:
    limits = AgentLoopLimits.default()

    assert limits.max_tool_rounds == 200
    assert limits.max_provider_calls == 400
    assert limits.max_turn_seconds == 3600


def test_swe_lite_limits_match_goal_profile() -> None:
    limits = AgentLoopLimits.swe_lite()

    assert limits.max_tool_rounds == 60
    assert limits.max_provider_calls == 100
    assert limits.max_turn_seconds == 1800


def test_summary_limits_disable_tool_loops() -> None:
    limits = AgentLoopLimits.summary()

    assert limits.max_tool_rounds == 1
    assert limits.max_provider_calls == 3
    assert limits.max_turn_seconds == 120


def test_legacy_max_tool_rounds_override() -> None:
    limits = AgentLoopLimits.default().with_max_tool_rounds(4)

    assert limits.max_tool_rounds == 4


def test_stop_reason_values_are_finish_reasons() -> None:
    assert AgentLoopStopReason.PROVIDER_CALL_LIMIT.value == "provider_call_limit"
    assert AgentLoopStopReason.TURN_TIMEOUT.value == "turn_timeout"
    assert AgentLoopStopReason.TOOL_ROUND_LIMIT.value == "tool_round_limit"
