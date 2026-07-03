"""项目内部复用的路径沙箱。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.utils.sandbox_access import SandboxAccess


class PathSandbox:
    """限制工具只能访问指定根目录内的路径。"""

    def __init__(self, root: str | Path, *, access: SandboxAccess | None = None) -> None:
        self.root = Path(root).resolve()
        self.access = access or SandboxAccess()

    def resolve(self, path: str | Path | None = None) -> Path:
        """把输入路径解析成沙箱内的绝对路径。"""

        if path in (None, ""):
            target = self.root
        else:
            raw = Path(path)
            target = (raw if raw.is_absolute() else self.root / raw).resolve()

        if not self.access.unrestricted and target != self.root and self.root not in target.parents:
            raise ValueError(f"路径超出项目目录：{path}")
        return target

    def resolve_validated(
        self,
        path: str | Path | None = None,
        *,
        expect: str = "any",
    ) -> Path:
        """解析并校验路径：沙箱内 + 存在性 + 类型检查。

        expect 取值: "any"（默认，只检查存在）、"file"、"dir"。
        校验失败时抛出 ValueError，方便工具层直接捕获得到统一错误信息。
        """

        target = self.resolve(path)

        if not target.exists():
            raise ValueError(f"路径不存在：{path}")
        if expect == "file" and not target.is_file():
            raise ValueError(f"路径不是文件：{path}")
        if expect == "dir" and not target.is_dir():
            raise ValueError(f"路径不是目录：{path}")

        return target

    def relative(self, path: str | Path) -> str:
        """把沙箱内路径转换成相对项目根目录的 POSIX 风格路径。"""

        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            if self.access.unrestricted:
                return str(resolved)
            raise
