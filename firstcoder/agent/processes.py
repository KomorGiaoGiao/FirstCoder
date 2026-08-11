"""结构化长期进程管理。

与一次性 shell 工具不同，这一层把服务进程放进独立进程组，并把 stdout/stderr 写入
日志文件。FirstCoder CLI 退出后子进程仍可继续运行，Terminal-Bench verifier 因而能
检查真实服务状态；TUI 正常卸载时则会显式回收仍由当前 app 管理的进程。
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from firstcoder.utils.subprocess import process_group_kwargs, terminate_process_group
from firstcoder.utils.text import truncate_head_tail

PROCESS_RUNNING = "running"
PROCESS_EXITED = "exited"


@dataclass(slots=True)
class ManagedProcess:
    id: str
    command: str
    cwd: Path
    process: subprocess.Popen[str]
    stdout_path: Path
    stderr_path: Path
    label: str | None = None
    created_at: float = 0.0
    ready_pattern: str | None = None
    ready: bool = False

    def snapshot(self) -> dict[str, object]:
        returncode = self.process.poll()
        return {
            "process_id": self.id,
            "pid": self.process.pid,
            "command": self.command,
            "cwd": str(self.cwd),
            "label": self.label,
            "status": PROCESS_RUNNING if returncode is None else PROCESS_EXITED,
            "exit_code": returncode,
            "ready": self.ready,
            "ready_pattern": self.ready_pattern,
            "stdout_log": str(self.stdout_path),
            "stderr_log": str(self.stderr_path),
        }


@dataclass(frozen=True, slots=True)
class ProcessStartOutcome:
    process: ManagedProcess
    readiness_timed_out: bool = False
    exited_before_ready: bool = False


class ProcessManager:
    """管理当前 app 启动的长期进程及其日志。"""

    def __init__(
        self,
        *,
        log_root: str | Path,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.log_root = Path(log_root).resolve()
        self.log_root.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._processes: dict[str, ManagedProcess] = {}
        self._counter = 0

    def start(
        self,
        command: str,
        *,
        cwd: Path,
        env: dict[str, str],
        label: str | None = None,
        ready_pattern: str | None = None,
        ready_timeout_seconds: float = 10.0,
    ) -> ProcessStartOutcome:
        with self._lock:
            self._counter += 1
            process_id = f"proc_{self._counter:04d}"
        stdout_path = self.log_root / f"{process_id}.stdout.log"
        stderr_path = self.log_root / f"{process_id}.stderr.log"
        stdout_handle = stdout_path.open("a", encoding="utf-8", buffering=1)
        stderr_handle = stderr_path.open("a", encoding="utf-8", buffering=1)
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                shell=True,
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                encoding="utf-8",
                errors="replace",
                **process_group_kwargs(),
            )
        finally:
            # 子进程已经继承独立文件句柄；父进程不保留写端，避免 CLI 退出时影响服务。
            stdout_handle.close()
            stderr_handle.close()

        managed = ManagedProcess(
            id=process_id,
            command=command,
            cwd=cwd,
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            label=label.strip() if label and label.strip() else None,
            created_at=self._clock(),
            ready_pattern=ready_pattern.strip() if ready_pattern and ready_pattern.strip() else None,
        )
        with self._lock:
            self._processes[managed.id] = managed

        if managed.ready_pattern is None:
            return ProcessStartOutcome(process=managed)
        deadline = self._clock() + ready_timeout_seconds
        while self._clock() < deadline:
            if self._matches_readiness(managed):
                managed.ready = True
                return ProcessStartOutcome(process=managed)
            if managed.process.poll() is not None:
                return ProcessStartOutcome(process=managed, exited_before_ready=True)
            time.sleep(0.05)
        if self._matches_readiness(managed):
            managed.ready = True
            return ProcessStartOutcome(process=managed)
        return ProcessStartOutcome(
            process=managed,
            readiness_timed_out=managed.process.poll() is None,
            exited_before_ready=managed.process.poll() is not None,
        )

    def get(self, process_id: str) -> ManagedProcess | None:
        with self._lock:
            return self._processes.get(process_id)

    def list(self) -> list[ManagedProcess]:
        with self._lock:
            return list(self._processes.values())

    def logs(
        self,
        process_id: str,
        *,
        stream: str = "both",
        max_chars: int = 20000,
    ) -> tuple[str, bool]:
        managed = self.get(process_id)
        if managed is None:
            raise KeyError(process_id)
        sections: list[str] = []
        if stream in {"both", "stdout"}:
            sections.append("stdout:\n" + _read_log(managed.stdout_path))
        if stream in {"both", "stderr"}:
            sections.append("stderr:\n" + _read_log(managed.stderr_path))
        return truncate_head_tail("\n\n".join(sections).rstrip(), max_chars)

    def stop(self, process_id: str) -> ManagedProcess | None:
        managed = self.get(process_id)
        if managed is None:
            return None
        if managed.process.poll() is None:
            terminate_process_group(managed.process)
        return managed

    def shutdown(self) -> None:
        for managed in self.list():
            if managed.process.poll() is None:
                terminate_process_group(managed.process)

    @staticmethod
    def _matches_readiness(managed: ManagedProcess) -> bool:
        pattern = managed.ready_pattern
        if pattern is None:
            return True
        return pattern in _read_log(managed.stdout_path) or pattern in _read_log(managed.stderr_path)


def _read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
