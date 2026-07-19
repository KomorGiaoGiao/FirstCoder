"""项目级模型选择偏好持久化。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelSelectionState:
    """最近一次有效模型选择及其最近使用列表。"""

    last_selected: str | None = None
    recent: tuple[str, ...] = ()


class ModelStateStore:
    """以原子 JSON 文件保存项目级模型选择。

    状态文件只是 UI 偏好，任何读取或写入异常都不应阻止应用启动。
    """

    def __init__(self, path: Path, *, recent_limit: int = 10) -> None:
        if recent_limit <= 0:
            raise ValueError("recent_limit must be positive")
        self.path = Path(path)
        self.recent_limit = recent_limit

    def load(self) -> ModelSelectionState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            return ModelSelectionState()
        if not isinstance(raw, dict):
            return ModelSelectionState()

        last_selected = raw.get("last_selected")
        recent = raw.get("recent", [])
        if last_selected is not None and (not isinstance(last_selected, str) or not last_selected.strip()):
            return ModelSelectionState()
        if not isinstance(recent, list) or any(not isinstance(item, str) or not item.strip() for item in recent):
            return ModelSelectionState()

        values: list[str] = []
        for item in recent:
            if item not in values:
                values.append(item)
        if last_selected is not None and last_selected not in values:
            values.insert(0, last_selected)
        return ModelSelectionState(
            last_selected=last_selected,
            recent=tuple(values[: self.recent_limit]),
        )

    def record_selection(self, ref: str) -> ModelSelectionState:
        if not isinstance(ref, str) or not ref.strip():
            return self.load()
        old = self.load()
        recent = [ref, *old.recent]
        deduped: list[str] = []
        for item in recent:
            if item not in deduped:
                deduped.append(item)
        state = ModelSelectionState(ref, tuple(deduped[: self.recent_limit]))
        self._write(state)
        return state

    def _write(self, state: ModelSelectionState) -> None:
        temporary_path: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                json.dump(
                    {"last_selected": state.last_selected, "recent": list(state.recent)},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            Path(temporary_path).replace(self.path)
        except OSError:
            # A preference write failure should not break a model switch.
            return
        finally:
            if temporary_path is not None:
                try:
                    Path(temporary_path).unlink(missing_ok=True)
                except OSError:
                    pass


__all__ = ["ModelSelectionState", "ModelStateStore"]
