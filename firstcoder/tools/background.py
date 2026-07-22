"""后台任务控制面工具：background_status / background_cancel。

这两个工具让模型可以查询或取消自己启动的后台任务。它们本身是同步、只读/控制类
操作，不应该再被后台化，因此不进入 background 允许列表。
"""

from __future__ import annotations

from firstcoder.agent.background import BackgroundJobManager
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.schema import object_schema, property_schema


def create_background_status_tool(
    manager: BackgroundJobManager,
    *,
    session_id: str | None = None,
) -> Tool:
    """创建查询后台任务状态的工具。"""

    def background_status(job_id: str | None = None) -> ToolResult:
        if job_id:
            job = manager.get(job_id, session_id=session_id)
            if job is None:
                return make_error_result("background_status", f"未找到后台任务：{job_id}")
            snapshot = job.snapshot()
            return make_text_result(
                "background_status",
                _format_job_line(snapshot),
                job=snapshot,
            )
        jobs = [job.snapshot() for job in manager.list(session_id=session_id)]
        if not jobs:
            return make_text_result("background_status", "当前没有后台任务。", jobs=[])
        lines = [_format_job_line(job) for job in jobs]
        return make_text_result("background_status", "\n".join(lines), jobs=jobs)

    return Tool(
        definition=ToolDefinition(
            name="background_status",
            description=("Inspect background jobs started with run_in_background. Omit job_id to list " "all jobs, or pass a job_id to see one job's status and result availability."),
            parameters=object_schema(
                {
                    "job_id": property_schema(
                        "string",
                        description="Optional background job id, e.g. bg_0001.",
                    ),
                },
            ),
        ),
        executor=background_status,
    )


def create_background_cancel_tool(
    manager: BackgroundJobManager,
    *,
    session_id: str | None = None,
) -> Tool:
    """创建取消后台任务的工具。"""

    def background_cancel(job_id: str) -> ToolResult:
        job = manager.cancel(job_id, session_id=session_id)
        if job is None:
            return make_error_result("background_cancel", f"未找到后台任务：{job_id}")
        return make_text_result(
            "background_cancel",
            f"Background job {job.id} is now {job.status}" + (" (cancellation requested)." if job.cancel_requested else "."),
            job=job.snapshot(),
        )

    return Tool(
        definition=ToolDefinition(
            name="background_cancel",
            description=("Request cancellation of a background job by id. Jobs that already finished " "are returned unchanged; running jobs stop only if the tool honours cancellation."),
            parameters=object_schema(
                {
                    "job_id": property_schema(
                        "string",
                        description="Background job id to cancel, e.g. bg_0001.",
                    ),
                },
                required=["job_id"],
            ),
        ),
        executor=background_cancel,
    )


def _format_job_line(snapshot: dict[str, object]) -> str:
    label = snapshot.get("label")
    label_hint = f" [{label}]" if label else ""
    return f"{snapshot.get('job_id')}{label_hint}: {snapshot.get('tool_name')} -> {snapshot.get('status')}"
