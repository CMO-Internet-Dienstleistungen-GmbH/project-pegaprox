"""What PegaProx is asking a Hyper-V host right now, in the shape the task bar reads.

Every script sent to a host costs seconds, and before this register existed none of it was
visible: a preflight that took a minute looked exactly like one that had hung, and a call
waiting for the host's session looked exactly like one that was running. The task bar is
where an operator already looks for "what is this system doing", so the calls go there.

The register is memory only and says nothing about the past beyond a few minutes. It is
not an audit log -- the audit log records what was changed on a host, this records what
is being waited for. A restart empties it, which is correct: nothing is waiting any more.

How a call is tracked, from the outside in:

- `HyperVManager` opens a task around each script with `track()`, naming what the script
  does and which VM it concerns. The task starts as `queued`.
- The transport calls `mark_running()` once it holds a session, so the time spent waiting
  for one stays visible as waiting and is not folded into the run time.
- Leaving `track()` finishes the task as `OK` or `error`.

The transport learns which task it is serving through a context variable, not an
argument, so no signature between the manager and the transport had to change for it.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from pegaprox.core.hyperv_errors import HyperVError

STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_OK = 'OK'
STATUS_ERROR = 'error'

#: The name a call is filed under when no person asked for it: the migration runner, the
#: inventory refresh, the connection check at start-up.
SYSTEM_USER = 'pegaprox'

#: How long a finished call stays in the list. Long enough to see what a preflight did after
#: it returned, short enough that the list shows the present and not a history.
FINISHED_RETENTION_SECONDS = 600

#: Hard ceiling per host. An inventory walk and a busy wizard produce a few calls a second
#: at most; the cap only matters if something loops, and then it keeps memory bounded.
MAX_TASKS_PER_HOST = 200

#: Upper bound on the error text a task keeps. The message is already redacted by the
#: transport; this only keeps a stack of PowerShell errors from filling the list.
_MAX_ERROR_LENGTH = 500

_UPID_PREFIX = 'hvq:'


@dataclass
class HyperVTask:
    """One script call against one host."""

    upid: str
    host_id: str
    task_type: str
    vm_guid: str
    user: str
    queued_at: float
    status: str = STATUS_QUEUED
    started_at: float | None = None
    ended_at: float | None = None
    error: str = ''
    # Resolved lazily by whoever turns the task into a row: the transport knows a GUID,
    # the task bar needs the synthetic VMID, and the mapping lives in the database.
    vmid: int | None = field(default=None)

    @property
    def finished(self) -> bool:
        return self.status in (STATUS_OK, STATUS_ERROR)


_lock = threading.Lock()
_tasks: dict[str, list[HyperVTask]] = {}
_current: contextvars.ContextVar[HyperVTask | None] = contextvars.ContextVar(
    'hyperv_current_task', default=None)


def current_user() -> str:
    """The person on whose behalf the current code runs, or `pegaprox` when nobody is.

    Read from the request the authentication layer already decorated, rather than handed
    down through every signature between a route and the transport. Outside a request --
    a runner thread, the start-up connection, a background refresh -- there is nobody,
    and the call is filed under the system.
    """
    try:
        from flask import has_request_context, request
    except ImportError:                                    # pragma: no cover - flask is a dependency
        return SYSTEM_USER
    if not has_request_context():
        return SYSTEM_USER
    session = getattr(request, 'session', None) or {}
    return session.get('user') or SYSTEM_USER


@contextmanager
def track(host_id: str, task_type: str, vm_guid: str = ''):
    """Record one call from the moment it wants a session until it has an answer.

    Nested use does not open a second task: a manager method that calls another manager
    method is still one thing the operator asked for, and two rows for it would read as
    two calls to the host.
    """
    if _current.get() is not None:
        yield _current.get()
        return

    task = HyperVTask(upid=f'{_UPID_PREFIX}{uuid.uuid4()}', host_id=host_id,
                      task_type=task_type, vm_guid=vm_guid or '', user=current_user(),
                      queued_at=time.time())
    with _lock:
        _tasks.setdefault(host_id, []).append(task)
        _prune_locked(host_id, task.queued_at)
    token = _current.set(task)
    try:
        yield task
    except BaseException as exc:
        _finish(task, STATUS_ERROR, _error_text(exc))
        raise
    else:
        _finish(task, STATUS_OK, '')
    finally:
        _current.reset(token)


def mark_running() -> None:
    """The transport holds a session for the current task. A no-op outside `track()`."""
    task = _current.get()
    if task is None:
        return
    with _lock:
        if task.status == STATUS_QUEUED:
            task.status = STATUS_RUNNING
            task.started_at = time.time()


def tasks_for(host_id: str) -> list[HyperVTask]:
    """This host's tasks, newest first, with anything past its retention dropped."""
    with _lock:
        _prune_locked(host_id, time.time())
        return sorted(_tasks.get(host_id, ()), key=lambda t: t.queued_at, reverse=True)


def find(host_id: str, upid: str) -> HyperVTask | None:
    """One task of this host by its UPID, or None once it has been dropped."""
    for task in tasks_for(host_id):
        if task.upid == upid:
            return task
    return None


def forget_host(host_id: str) -> None:
    """Drop every task of a host that was removed."""
    with _lock:
        _tasks.pop(host_id, None)


def summary_lines(task: HyperVTask) -> list[str]:
    """What the task log shows for a Hyper-V call.

    There is no host-side log to read: the script ran inside a PowerShell session that is
    gone. What is known is what it was, how long it waited and ran, and how it ended.
    """
    lines = [f'Hyper-V call: {task.task_type}']
    if task.vm_guid:
        lines.append(f'VM: {task.vm_guid}')
    lines.append(f'Requested by: {task.user}')
    waited = (task.started_at or task.ended_at or time.time()) - task.queued_at
    lines.append(f'Waited for a session: {waited:.1f} s')
    if task.started_at is not None:
        ran = (task.ended_at or time.time()) - task.started_at
        lines.append(f'Ran: {ran:.1f} s')
    lines.append(f'Status: {task.status}')
    if task.error:
        lines.append(f'Error: {task.error}')
    return lines


def _finish(task: HyperVTask, status: str, error: str) -> None:
    with _lock:
        task.status = status
        task.ended_at = time.time()
        task.error = error


def _error_text(exc: BaseException) -> str:
    """The error as the task keeps it.

    A HyperVError carries a message the transport has already redacted. Anything else
    came from somewhere that promised no such thing, so only its type is kept -- a
    traceback text can hold the host name or an account, and the task bar is shown to
    everyone who can see the host.
    """
    if isinstance(exc, HyperVError):
        return exc.message[:_MAX_ERROR_LENGTH]
    return f'{type(exc).__name__} (see the server log)'


def _prune_locked(host_id: str, now: float) -> None:
    tasks = _tasks.get(host_id)
    if not tasks:
        return
    kept = [t for t in tasks
            if not t.finished or (now - (t.ended_at or now)) < FINISHED_RETENTION_SECONDS]
    if len(kept) > MAX_TASKS_PER_HOST:
        # Finished ones go first; a call still waiting or running is never dropped from
        # view, because that is the one somebody is looking for.
        finished = [t for t in kept if t.finished]
        excess = len(kept) - MAX_TASKS_PER_HOST
        drop = {id(t) for t in sorted(finished, key=lambda t: t.ended_at or 0)[:excess]}
        kept = [t for t in kept if id(t) not in drop]
    _tasks[host_id] = kept
