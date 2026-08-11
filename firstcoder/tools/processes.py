"""结构化长期进程工具。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.agent.processes import ProcessManager
from firstcoder.permissions.types import PermissionAction
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result, make_text_result
from firstcoder.utils.execution_sandbox import ExecutionSandbox
from firstcoder.utils.sandbox_access import SandboxAccess
from firstcoder.utils.schema import object_schema, property_schema


def create_process_tools(
    root: str | Path,
    manager: ProcessManager,
    *,
    access: SandboxAccess | None = None,
) -> list[Tool]:
    sandbox = ExecutionSandbox(root, access=access)

    def process_start(
        command: str,
        cwd: str = ".",
        env: dict[str, str] | None = None,
        label: str | None = None,
        ready_pattern: str | None = None,
        ready_timeout_seconds: int = 10,
    ) -> ToolResult:
        """启动长期进程，可等待 stdout/stderr 中出现 readiness 文本。"""

        if not command.strip():
            return make_error_result("process_start", "command 不能为空")
        if ready_timeout_seconds <= 0:
            return make_error_result("process_start", "ready_timeout_seconds 必须大于 0")
        try:
            workdir = sandbox.resolve_cwd(cwd)
            accepted_env, rejected_env = sandbox.prepare_env_overrides(env)
            if rejected_env:
                return make_error_result(
                    "process_start",
                    "拒绝传入敏感环境变量：" + ", ".join(rejected_env),
                    rejected_env_keys=list(rejected_env),
                )
            outcome = manager.start(
                command,
                cwd=workdir,
                env=sandbox.build_env(accepted_env),
                label=label,
                ready_pattern=ready_pattern,
                ready_timeout_seconds=ready_timeout_seconds,
            )
        except (OSError, ValueError) as exc:
            return make_error_result("process_start", f"长期进程启动失败：{exc}")

        snapshot = outcome.process.snapshot()
        data = {
            "process": snapshot,
            "env_keys": sorted(accepted_env),
            "rejected_env_keys": sorted(rejected_env),
        }
        if outcome.exited_before_ready:
            return make_error_result(
                "process_start",
                f"进程 {outcome.process.id} 在 readiness 条件满足前退出。请查看 process_logs。",
                **data,
                readiness_failed=True,
            )
        if outcome.readiness_timed_out:
            return make_error_result(
                "process_start",
                f"进程 {outcome.process.id} 仍在运行，但 {ready_timeout_seconds} 秒内未出现 readiness 文本。"
                "请检查 process_logs/process_status，必要时停止后修正启动命令。",
                **data,
                readiness_timed_out=True,
            )
        readiness = "并已满足 readiness 条件" if ready_pattern else ""
        return make_text_result(
            "process_start",
            f"长期进程 {outcome.process.id} 已启动{readiness}。",
            **data,
        )

    start_tool = Tool(
        definition=ToolDefinition(
            name="process_start",
            description=(
                "Start a long-lived service or daemon in its own process group. Logs are persisted; "
                "optionally wait for a readiness substring before continuing. Prefer this over shell "
                "background syntax for servers, watchers, SSH daemons, and emulators."
            ),
            parameters=object_schema(
                {
                    "command": property_schema("string", description="Shell command to start."),
                    "cwd": property_schema("string", description="Workspace-relative working directory."),
                    "env": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Non-sensitive environment overrides for this process.",
                    },
                    "label": property_schema("string", description="Optional short process label."),
                    "ready_pattern": property_schema(
                        "string",
                        description="Optional literal text expected in stdout/stderr before the service is ready.",
                    ),
                    "ready_timeout_seconds": property_schema(
                        "integer",
                        description="Seconds to wait for ready_pattern; default 10.",
                    ),
                },
                required=["command"],
            ),
        ),
        executor=process_start,
        permission=ToolPermissionSpec(
            action=PermissionAction.EXECUTE_SHELL,
            target_arg="command",
            cwd_arg="cwd",
            reason="启动长期进程需要用户确认。",
        ),
    )

    def process_status(process_id: str | None = None) -> ToolResult:
        """查看一个或全部长期进程状态。"""

        if process_id:
            managed = manager.get(process_id)
            if managed is None:
                return make_error_result("process_status", f"未找到长期进程：{process_id}")
            snapshot = managed.snapshot()
            return make_text_result("process_status", _format_process(snapshot), process=snapshot)
        snapshots = [managed.snapshot() for managed in manager.list()]
        if not snapshots:
            return make_text_result("process_status", "当前没有长期进程。", processes=[])
        return make_text_result(
            "process_status",
            "\n".join(_format_process(snapshot) for snapshot in snapshots),
            processes=snapshots,
        )

    def process_logs(
        process_id: str,
        stream: str = "both",
        max_chars: int = 20000,
    ) -> ToolResult:
        """读取长期进程持久化日志。"""

        if stream not in {"both", "stdout", "stderr"}:
            return make_error_result("process_logs", "stream 必须是 both、stdout 或 stderr")
        if max_chars <= 0:
            return make_error_result("process_logs", "max_chars 必须大于 0")
        try:
            content, truncated = manager.logs(process_id, stream=stream, max_chars=max_chars)
        except KeyError:
            return make_error_result("process_logs", f"未找到长期进程：{process_id}")
        return make_text_result(
            "process_logs",
            content or "进程尚未产生日志。",
            process_id=process_id,
            stream=stream,
            truncated=truncated,
        )

    def process_stop(process_id: str) -> ToolResult:
        """停止一个由 FirstCoder 启动的长期进程及其子进程。"""

        managed = manager.stop(process_id)
        if managed is None:
            return make_error_result("process_stop", f"未找到长期进程：{process_id}")
        snapshot = managed.snapshot()
        return make_text_result(
            "process_stop",
            f"长期进程 {process_id} 已停止，退出码：{snapshot['exit_code']}。",
            process=snapshot,
        )

    return [
        start_tool,
        Tool(
            definition=ToolDefinition(
                name="process_status",
                description="Inspect one or all long-lived processes started by process_start.",
                parameters=object_schema(
                    {"process_id": property_schema("string", description="Optional process id, e.g. proc_0001.")}
                ),
            ),
            executor=process_status,
        ),
        Tool(
            definition=ToolDefinition(
                name="process_logs",
                description="Read bounded stdout/stderr logs for a process_start process.",
                parameters=object_schema(
                    {
                        "process_id": property_schema("string"),
                        "stream": {
                            "type": "string",
                            "enum": ["both", "stdout", "stderr"],
                        },
                        "max_chars": property_schema("integer"),
                    },
                    required=["process_id"],
                ),
            ),
            executor=process_logs,
        ),
        Tool(
            definition=ToolDefinition(
                name="process_stop",
                description="Stop a process_start process and its descendants.",
                parameters=object_schema(
                    {"process_id": property_schema("string")},
                    required=["process_id"],
                ),
            ),
            executor=process_stop,
        ),
    ]


def _format_process(snapshot: dict[str, object]) -> str:
    label = f" [{snapshot['label']}]" if snapshot.get("label") else ""
    readiness = " ready" if snapshot.get("ready") else ""
    return (
        f"{snapshot['process_id']}{label}: pid={snapshot['pid']} "
        f"status={snapshot['status']}{readiness} exit_code={snapshot['exit_code']}"
    )
