# Yuren Model Topbar Themes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the three named Yuren models distinct animated topbar colour themes.

**Architecture:** Keep the existing markup and timer architecture. Introduce a model-to-palette lookup in `firstcoder/app/tui.py`; only an exact `Yuren` provider and an exact supported model resolve a palette. Tests exercise the markup output and timer eligibility without changing request-side provider code.

**Tech Stack:** Python, Textual, Rich markup, pytest.

---

### Task 1: Specify theme selection with failing tests

**Files:**
- Modify: `tests/test_app_tui.py:381-410`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(
    ("model", "colour"),
    [
        ("gpt-5.6-terra", "#18cfcb"),
        ("gpt-5.6-sol", "#ff5c3d"),
        ("gpt-5.6-luna", "#b9c8ff"),
    ],
)
def test_supported_yuren_models_use_distinct_moving_colour_bands(model: str, colour: str) -> None:
    first = _provider_model_markup("Yuren", model, glow_frame=0)
    next_frame = _provider_model_markup("Yuren", model, glow_frame=1)

    assert Text.from_markup(first).plain == f"Yuren/{model}"
    assert first != next_frame
    assert f"[{colour}]" in first


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_unsupported_yuren_model_does_not_start_provider_glow() -> None:
    app = FirstCoderApp(config=FirstCoderTuiConfig(provider_name="Yuren", provider_model="other-model"))

    async with app.run_test():
        assert app._provider_glow_timer is None
```

- [ ] **Step 2: Run the targeted test file and verify failure**

Run: `.venv/bin/python -m pytest tests/test_app_tui.py -q`

Expected: the Sol and Luna markup assertions fail because the code still uses the Terra palette for every Yuren model, and the unsupported-model timer assertion fails.

### Task 2: Add palette lookup and timer gating

**Files:**
- Modify: `firstcoder/app/tui.py:68-72`
- Modify: `firstcoder/app/tui.py:1048-1074`
- Modify: `firstcoder/app/tui.py:1395-1414`

- [ ] **Step 1: Add supported palettes and a lookup helper**

```python
_YUREN_MODEL_GLOW_PALETTES = {
    "gpt-5.6-terra": ("#b8ffdf", "#81e8bb", "#18cfcb", "#45e6df", "#5fb5ff"),
    "gpt-5.6-sol": ("#ffb347", "#ff7a45", "#ff5c3d", "#e9422e", "#ffd166"),
    "gpt-5.6-luna": ("#f4f6ff", "#c9d5ff", "#b9c8ff", "#a99ee8", "#d9e7ff"),
}


def _yuren_model_glow_palette(provider: str, model: str) -> tuple[str, ...] | None:
    if provider != "Yuren":
        return None
    return _YUREN_MODEL_GLOW_PALETTES.get(model)
```

- [ ] **Step 2: Use the lookup in the timer lifecycle and markup renderer**

```python
def _sync_provider_glow(self) -> None:
    if _yuren_model_glow_palette(self.config.provider_name, self.config.provider_model) is not None:
        self._start_provider_glow()
    else:
        self._stop_provider_glow()

def _provider_model_markup(provider: str, model: str, *, glow_frame: int = 0) -> str:
    palette = _yuren_model_glow_palette(provider, model)
    if palette is None:
        return f"{_provider_name_markup(provider, glow_frame=glow_frame)}[#6e6d72]/{escape(model)}[/]"
    return f"{_glow_markup(provider, glow_frame=glow_frame, palette=palette)}[#6e6d72]/[/]{_glow_markup(model, glow_frame=glow_frame + len(provider) + 1, palette=palette)}"
```

- [ ] **Step 3: Make `_glow_markup` accept the selected palette**

```python
def _glow_markup(text: str, *, glow_frame: int, palette: tuple[str, ...]) -> str:
    return "".join(
        f"[{palette[(index + glow_frame) % len(palette)]}]{escape(character)}[/]"
        for index, character in enumerate(text)
    )
```

- [ ] **Step 4: Run targeted tests and verify success**

Run: `.venv/bin/python -m pytest tests/test_app_tui.py -q`

Expected: all topbar and timer tests pass.

### Task 3: Verify integration hygiene

**Files:**
- Modify: `firstcoder/app/tui.py`
- Modify: `tests/test_app_tui.py`

- [ ] **Step 1: Run the focused UI test suite**

Run: `.venv/bin/python -m pytest tests/test_app_tui.py -q`

Expected: exit code 0.

- [ ] **Step 2: Check formatting and diff whitespace**

Run: `git diff --check`

Expected: exit code 0 with no output.
