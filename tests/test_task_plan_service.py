from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest

from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.planning.reducer import TaskPlanCommandError, TaskPlanRevisionConflict
from firstcoder.planning.service import TaskPlanMutation, TaskPlanService


def _service(tmp_path, *, session_id: str = "sess_plan") -> tuple[
    JsonlSessionStore,
    SessionEventWriter,
    TaskPlanService,
]:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id=session_id)
    writer.append_session_created()
    return store, writer, TaskPlanService(store=store, writer=writer)


def _plan_events(store: JsonlSessionStore, session_id: str = "sess_plan"):
    return [event for event in store.list_events(session_id) if event.type == "task_plan_updated"]


def test_create_returns_projection_and_writes_exactly_one_replayable_event(
    tmp_path,
) -> None:
    store, _, service = _service(tmp_path)

    mutation = service.create(
        mode="linear",
        expected_revision=0,
        tasks=[
            {"id": "inspect", "content": "Inspect"},
            {"id": "implement", "content": "Implement"},
        ],
    )

    assert isinstance(mutation, TaskPlanMutation)
    assert mutation.changed is True
    assert mutation.changes == tuple(task.to_dict() for task in mutation.plan.tasks)
    assert mutation.plan.revision == 1
    assert mutation.projection == {
        "mode": "linear",
        "revision": 1,
        "ready_task_ids": ["inspect"],
        "blocked_task_ids": ["implement"],
        "topological_levels": [["inspect"], ["implement"]],
        "tasks": [task.to_dict() for task in mutation.plan.tasks],
    }

    events = _plan_events(store)
    assert len(events) == 1
    assert events[0].payload == {
        "previous_revision": 0,
        "revision": 1,
        "operation": "create",
        "changes": [task.to_dict() for task in mutation.plan.tasks],
        "snapshot": mutation.plan.to_dict(),
    }
    assert store.rebuild_session_view("sess_plan").task_plan == mutation.plan
    assert service.current() == mutation.plan


def test_update_and_revise_each_write_one_event_with_reducer_changes(tmp_path) -> None:
    store, _, service = _service(tmp_path)
    service.create(
        mode="dag",
        expected_revision=0,
        tasks=[
            {"id": "inspect", "content": "Inspect"},
            {"id": "implement", "content": "Implement", "depends_on": ["inspect"]},
        ],
    )

    updated = service.update(
        expected_revision=1,
        updates=[{"id": "inspect", "status": "completed", "owner": "main"}],
    )
    revised = service.revise(
        expected_revision=2,
        revisions=[{"id": "implement", "content": "Implement and verify"}],
    )

    assert updated.changed is True
    assert updated.projection["ready_task_ids"] == ["implement"]
    assert revised.changed is True
    assert revised.plan.revision == 3
    events = _plan_events(store)
    assert len(events) == 3
    assert events[1].payload["operation"] == "update"
    assert events[1].payload["changes"] == [{"id": "inspect", "status": "completed", "owner": "main"}]
    assert events[2].payload["operation"] == "revise"
    assert events[2].payload["changes"] == [{"id": "implement", "content": "Implement and verify"}]
    assert store.rebuild_session_view("sess_plan").task_plan == revised.plan


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda service: service.create(mode="linear", expected_revision=1, tasks=[]),
            id="create",
        ),
        pytest.param(
            lambda service: service.update(
                expected_revision=1,
                updates=[{"id": "work", "owner": "main"}],
            ),
            id="update",
        ),
        pytest.param(
            lambda service: service.revise(
                expected_revision=1,
                revisions=[{"id": "work", "content": "Work"}],
            ),
            id="revise",
        ),
    ],
)
def test_no_op_returns_projection_without_writing_event(
    tmp_path,
    mutate: Callable[[TaskPlanService], TaskPlanMutation],
) -> None:
    store, _, service = _service(tmp_path)
    original = service.create(
        mode="linear",
        expected_revision=0,
        tasks=[{"id": "work", "content": "Work", "owner": "main"}],
    ).plan
    before = len(_plan_events(store))

    mutation = mutate(service)

    assert mutation.changed is False
    assert mutation.plan == original
    assert mutation.projection["revision"] == 1
    assert len(_plan_events(store)) == before


def test_each_mutation_replays_current_view_so_stale_revision_writes_nothing(
    tmp_path,
) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_plan")
    writer.append_session_created()
    first = TaskPlanService(store=store, writer=writer)
    stale = TaskPlanService(store=store, writer=writer)
    created = first.create(
        mode="linear",
        expected_revision=0,
        tasks=[{"id": "work", "content": "Work"}],
    )
    before = len(_plan_events(store))

    with pytest.raises(TaskPlanRevisionConflict) as caught:
        stale.create(
            mode="linear",
            expected_revision=0,
            tasks=[{"id": "later", "content": "Later"}],
        )

    assert caught.value.expected == 0
    assert caught.value.actual == 1
    assert stale.current() == created.plan
    assert len(_plan_events(store)) == before


def test_concurrent_services_atomically_compare_revision_and_append(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    writer_a = SessionEventWriter(store=store, session_id="sess_plan")
    writer_a.append_session_created()
    writer_b = SessionEventWriter(store=store, session_id="sess_plan")
    service_a = TaskPlanService(store=store, writer=writer_a)
    service_b = TaskPlanService(store=store, writer=writer_b)

    import firstcoder.planning.service as service_module

    original_create_tasks = service_module.create_tasks
    rendezvous = Barrier(2)

    def synchronized_create_tasks(**kwargs):
        try:
            rendezvous.wait(timeout=0.25)
        except BrokenBarrierError:
            pass
        return original_create_tasks(**kwargs)

    monkeypatch.setattr(service_module, "create_tasks", synchronized_create_tasks)

    def create(service: TaskPlanService, task_id: str):
        try:
            return service.create(
                mode="linear",
                expected_revision=0,
                tasks=[{"id": task_id, "content": task_id}],
            )
        except TaskPlanRevisionConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: create(*item),
                ((service_a, "a"), (service_b, "b")),
            )
        )

    assert sum(isinstance(result, TaskPlanMutation) for result in results) == 1
    assert sum(isinstance(result, TaskPlanRevisionConflict) for result in results) == 1
    assert len(_plan_events(store)) == 1
    assert store.rebuild_session_view("sess_plan").task_plan is not None


def test_invalid_batch_is_atomic_and_writes_no_event(tmp_path) -> None:
    store, _, service = _service(tmp_path)
    original = service.create(
        mode="dag",
        expected_revision=0,
        tasks=[
            {"id": "a", "content": "A"},
            {"id": "b", "content": "B"},
        ],
    ).plan
    before = len(_plan_events(store))

    with pytest.raises(TaskPlanCommandError, match="missing"):
        service.update(
            expected_revision=1,
            updates=[
                {"id": "a", "owner": "agent-1"},
                {"id": "b", "add_depends_on": ["missing"]},
            ],
        )

    assert len(_plan_events(store)) == before
    assert service.current() == original


@pytest.mark.parametrize("operation", ["update", "revise"])
def test_update_and_revise_without_a_plan_fail_cleanly_without_writing(
    tmp_path,
    operation: str,
) -> None:
    store, _, service = _service(tmp_path)

    with pytest.raises(TaskPlanCommandError, match="no current task plan"):
        if operation == "update":
            service.update(expected_revision=0, updates=[])
        else:
            service.revise(expected_revision=0, revisions=[])

    assert service.current() is None
    assert _plan_events(store) == []
