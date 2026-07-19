from pathlib import Path

from firstcoder.app.model_state import ModelSelectionState, ModelStateStore


def test_model_state_store_keeps_last_selected_and_deduplicated_recents(tmp_path: Path) -> None:
    store = ModelStateStore(tmp_path / "model_state.json")

    store.record_selection("yuren/gpt-main")
    store.record_selection("mimo/mimo-v2.5-pro")
    store.record_selection("yuren/gpt-main")

    state = store.load()
    assert state.last_selected == "yuren/gpt-main"
    assert state.recent == ("yuren/gpt-main", "mimo/mimo-v2.5-pro")


def test_model_state_store_treats_invalid_json_as_empty_state(tmp_path: Path) -> None:
    path = tmp_path / "model_state.json"
    path.write_text("{not-json", encoding="utf-8")

    assert ModelStateStore(path).load() == ModelSelectionState()


def test_model_state_store_limits_recent_models(tmp_path: Path) -> None:
    store = ModelStateStore(tmp_path / "model_state.json", recent_limit=2)
    for ref in ("a/one", "b/two", "c/three"):
        store.record_selection(ref)

    assert store.load().recent == ("c/three", "b/two")
