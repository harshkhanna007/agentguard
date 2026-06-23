"""AgentGuard v0.2 — a permission gate for AI agent tool calls.

Primitives, stdlib only, works with any agent or framework:

    AgentGuard().execute(name, params, executor)   gate a tool dispatcher (sync)
    await AgentGuard().aexecute(name, params, ...)  gate a tool dispatcher (async)
    @gate                                           gate a single function
    guard.pending() / guard.resolve(id, ok)         request-based approval (web UI)
    agentguard.log                                  JSONL audit of gated actions

The gate is code, not a prompt. An agent cannot talk its way past it.
Design bias: over-gating is safe, under-gating is dangerous — so detection
errs toward asking, and every error path fails *closed* (blocks).

v0.2 adds concurrent multi-agent safety: every approval is a uniquely
identified ``ApprovalRequest`` parked in a per-request registry, so two agents
asking at once can never be mixed up — approving one never touches the other.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

# Audit log location — override with AGENTGUARD_LOG=/path/to/file
LOG_FILE = os.environ.get("AGENTGUARD_LOG", "agentguard.log")

# Dangerous-keyword -> human risk label. Scanned most-severe first; the first
# hit wins. Substring match on the normalized name, so `bulk_delete`,
# `deleteUser` and `DELETE-RECORD` all trip the same rule.
_RISK_RULES = (
    (("delete", "drop", "wipe", "truncate", "purge", "destroy", "remove"),
     "IRREVERSIBLE - cannot be undone"),
    (("send", "email", "message", "notify", "publish", "post"),
     "EXTERNAL SEND - leaves your system"),
    (("execute", "run", "shell", "command", "exec", "eval", "spawn"),
     "SYSTEM EXECUTION - runs arbitrary code"),
    (("write", "overwrite", "update", "insert", "modify", "save"),
     "DATA MUTATION - changes stored state"),
)

_LOG_LOCK = threading.Lock()


def _normalize(name: object) -> str:
    """Lowercase and strip every non-alphanumeric char: delete_user -> deleteuser."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def classify(name: object) -> Optional[str]:
    """Return a risk label if the tool name looks dangerous, else None."""
    flat = _normalize(name)
    for keywords, label in _RISK_RULES:
        if any(kw in flat for kw in keywords):
            return label
    return None


# --- Public value objects -----------------------------------------------------

@dataclass(frozen=True)
class ApprovalRequest:
    """An immutable description of one dangerous action awaiting a decision.

    Frozen so it can be shared across threads / handed to a web layer without
    risk of mutation. ``request_id`` is the correlation key used everywhere:
    in the log, in :meth:`AgentGuard.pending`, and in :meth:`AgentGuard.resolve`.
    """

    request_id: str
    tool: str
    params: Any
    risk: str
    reason: Optional[str] = None
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    timestamp: str = ""
    # Read-only metadata: ISO time this request auto-fails-closed, so a UI can
    # render a countdown. Populated only in external mode when a timeout is set;
    # None otherwise. Does NOT drive timeout behavior — it just surfaces it.
    deadline: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Plain dict for JSON serialization (e.g. a /pending endpoint)."""
        return {
            "request_id": self.request_id,
            "tool": self.tool,
            "params": self.params,
            "risk": self.risk,
            "reason": self.reason,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "deadline": self.deadline,
        }


@dataclass(frozen=True)
class ActionDenied:
    """Returned (never raised) when an action is not approved.

    It is a *value*, not an exception, so agent frameworks that auto-retry on
    raised errors will treat it as an ordinary tool result and NOT retry it
    (Req. 7). It is falsy, so ``if guard.execute(...):`` reads naturally.
    """

    reason: str = "user_denied"
    tool: Optional[str] = None
    request_id: Optional[str] = None

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return f"Blocked by AgentGuard ({self.reason})"

    def to_dict(self) -> Dict[str, Any]:
        return {"denied": True, "reason": self.reason,
                "tool": self.tool, "request_id": self.request_id}


class ApprovalState(Enum):
    """Lifecycle of one parked request. Internal to the store."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class ResolveOutcome(Enum):
    """What :meth:`AgentGuard.resolve` did.

    Backward compatible with the old ``bool`` return: only ``RESOLVED`` is
    truthy, so existing ``if guard.resolve(...):`` code still means
    "I just resolved it". The others are falsy.
    """

    RESOLVED = "resolved"                  # THIS call decided it (first wins)
    ALREADY_RESOLVED = "already_resolved"  # decided already — safe idempotent no-op
    UNKNOWN = "unknown"                    # no such request_id
    EXPIRED = "expired"                    # deadline passed; already failed closed

    def __bool__(self) -> bool:
        return self is ResolveOutcome.RESOLVED


# --- Terminal rendering (degrades to ASCII on legacy code pages) --------------

def _supports_unicode() -> bool:
    enc = getattr(sys.stdout, "encoding", None) or ""
    try:
        "⚠━".encode(enc)
        return True
    except (LookupError, UnicodeEncodeError, TypeError):
        return False


_WARN = "⚠️  " if _supports_unicode() else "[!] "
_BAR = ("━" * 51) if _supports_unicode() else ("=" * 51)


def _show(params: Any) -> str:
    try:
        return json.dumps(params, default=str, ensure_ascii=False)
    except Exception:
        return repr(params)


class _TerminalConfirm:
    """The default approval callback: a y/n terminal prompt.

    Used when ``AgentGuard()`` is built with no confirm and no mode. It is an
    ordinary confirm callback — not a special mode or code path — so it flows
    through the same callback branch as any other approver. It serializes its
    own prompts (its own lock) so concurrent agents don't interleave on the
    shared console. No stdin (e.g. a headless process) -> returns False
    (fail closed).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __call__(self, request: ApprovalRequest) -> bool:
        with self._lock:
            lines = [
                "", _WARN + "AGENTGUARD: Action requires approval", _BAR,
                "Tool      : " + str(request.tool),
                "Parameters: " + _show(request.params),
                "Risk      : " + str(request.risk),
            ]
            if request.reason:
                lines.append("Reason    : " + str(request.reason))
            if request.agent_id:
                lines.append("Agent     : " + str(request.agent_id))
            if request.session_id:
                lines.append("Session   : " + str(request.session_id))
            lines.append("Request   : " + request.request_id)
            lines.append(_BAR)
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
            try:
                return input("Allow? (y/n): ").strip().lower() in ("y", "yes")
            except (EOFError, KeyboardInterrupt, OSError):
                sys.stdout.write("No interactive input available -> blocked (fail-closed).\n")
                sys.stdout.flush()
                return False


# The default approver used by AgentGuard() with no confirm and no mode.
_DEFAULT_CONFIRM = _TerminalConfirm()


def _log(tool: object, params: Any, decision: str, *,
         reason: Optional[str] = None, agent_id: Optional[str] = None,
         session_id: Optional[str] = None, request_id: Optional[str] = None,
         actor: Optional[str] = None) -> None:
    """Append one JSON line per gated action. Logging never raises."""
    entry: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": str(tool),
        "params": params,
        "decision": decision,
    }
    for key, value in (("reason", reason), ("agent_id", agent_id),
                       ("session_id", session_id), ("request_id", request_id),
                       ("actor", actor)):
        if value is not None:
            entry[key] = value
    try:
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with _LOG_LOCK, open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # an audit failure must never crash the agent


def _confirm_is_legacy(confirm: Callable) -> bool:
    """True if ``confirm`` is the v0.1 ``confirm(tool, params)`` two-arg form.

    A single-arg callable is the v0.2 ``confirm(request)`` form. If the
    signature can't be introspected, assume the new (single-arg) form.
    """
    try:
        sig = inspect.signature(confirm)
    except (TypeError, ValueError):
        return False
    positional = [
        p for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                      inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


class _Pending:
    """Internal record for one in-flight request: the public request, the
    in-process waiter that unblocks execute()/aexecute(), and the mutable
    decision state. A custom :class:`ApprovalStore` stores and returns these
    objects as-is — note ``event`` is an in-process primitive (see the
    cross-process caveat on :class:`ApprovalStore`)."""

    __slots__ = ("request", "event", "decision", "state", "actor",
                 "deadline_monotonic")

    def __init__(self, request: ApprovalRequest,
                 deadline_monotonic: Optional[float] = None) -> None:
        self.request = request
        self.event = threading.Event()        # set when a decision arrives
        self.decision: Optional[bool] = None
        self.state = ApprovalState.PENDING
        self.actor: Optional[str] = None
        self.deadline_monotonic = deadline_monotonic  # None => never auto-expires


class ApprovalStore(Protocol):
    """Pluggable persistence for parked approvals (Req. 5).

    The default is :class:`InMemoryStore`. Implement this Protocol to back
    approvals with your own store. ``decide`` MUST be an atomic compare-and-set
    so two near-simultaneous resolutions of the same id can never both win.

    CROSS-PROCESS CAVEAT: records carry an in-process ``threading.Event`` that
    AgentGuard waits on. A store that spans processes must ALSO arrange to wake
    the waiting process (polling, pub/sub, …); that wake-up is NOT provided
    here. See the README — out of the box this works single-process only.
    """

    def put(self, record: _Pending) -> None: ...
    def get(self, request_id: str) -> Optional[_Pending]: ...
    def list_pending(self) -> List[ApprovalRequest]: ...
    def decide(self, request_id: str, approved: bool,
               actor: Optional[str]) -> ResolveOutcome: ...
    def remove(self, request_id: str) -> None: ...


class InMemoryStore:
    """Default store: a dict guarded by a lock — the same mechanism v0.2 used,
    now behind the :class:`ApprovalStore` seam. Single process only.

    Resolved/expired records are retained (not evicted) so duplicate
    resolutions stay idempotent and :meth:`decide` can report
    ALREADY_RESOLVED / EXPIRED accurately. Volume is bounded by the number of
    gated actions; a custom store may add eviction.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, _Pending] = {}

    def put(self, record: _Pending) -> None:
        with self._lock:
            self._records[record.request.request_id] = record

    def get(self, request_id: str) -> Optional[_Pending]:
        with self._lock:
            return self._records.get(request_id)

    def list_pending(self) -> List[ApprovalRequest]:
        now = time.monotonic()
        with self._lock:
            return [r.request for r in self._records.values()
                    if r.state is ApprovalState.PENDING
                    and (r.deadline_monotonic is None
                         or now < r.deadline_monotonic)]

    def decide(self, request_id: str, approved: bool,
               actor: Optional[str]) -> ResolveOutcome:
        """Atomic compare-and-set. First caller to find PENDING wins."""
        with self._lock:
            rec = self._records.get(request_id)
            if rec is None:
                return ResolveOutcome.UNKNOWN
            if rec.state is not ApprovalState.PENDING:
                return (ResolveOutcome.EXPIRED
                        if rec.state is ApprovalState.EXPIRED
                        else ResolveOutcome.ALREADY_RESOLVED)
            if (rec.deadline_monotonic is not None
                    and time.monotonic() >= rec.deadline_monotonic):
                rec.state = ApprovalState.EXPIRED          # lazily mark; fail closed
                return ResolveOutcome.EXPIRED
            rec.decision = bool(approved)
            rec.actor = actor
            rec.state = (ApprovalState.APPROVED if approved
                         else ApprovalState.DENIED)
            return ResolveOutcome.RESOLVED

    def remove(self, request_id: str) -> None:
        with self._lock:
            self._records.pop(request_id, None)


class AgentGuard:
    """Gate a tool dispatcher, safe under many concurrent agents.

        guard = AgentGuard()                      # terminal y/n (default)
        guard = AgentGuard(confirm=my_callback)   # sync callback (Slack/web/etc.)
        guard = AgentGuard(mode="external")        # park requests; resolve() later

    Then change one line in your loop:
        result = guard.execute(tool_name, params, executor=run_tool,
                               agent_id="A", session_id="run-7")

    Web UI / async approval: requests created in ``mode="external"`` are visible
    via :meth:`pending` and answered via :meth:`resolve` — from any thread, in
    any order. Each request waits on its own event, so answering one never
    affects another. Pass ``on_request`` to be pushed each new request the
    instant it is parked, and ``store`` to plug in your own persistence.

    NOTE: external-mode approval works only when the code calling
    :meth:`execute` / :meth:`aexecute` and the code calling :meth:`resolve` run
    in the SAME process. Cross-process / distributed deployments need a custom
    :class:`ApprovalStore` with its own cross-process wake-up (not provided).
    """

    def __init__(self, confirm: Optional[Callable] = None, *,
                 mode: Optional[str] = None,
                 timeout: Optional[float] = None,
                 on_request: Optional[Callable[[ApprovalRequest], Any]] = None,
                 store: Optional[ApprovalStore] = None,
                 dangerous_tools: Optional[List[str]] = None) -> None:
        # Terminal approval is no longer a separate mode: with no confirm and no
        # (or "terminal") mode, use the built-in terminal y/n confirm CALLBACK.
        if confirm is None and mode in (None, "terminal"):
            confirm = _DEFAULT_CONFIRM
            mode = None
        self._confirm = confirm
        if confirm is not None:
            self._mode = "callback"
            self._confirm_legacy = _confirm_is_legacy(confirm)
        else:
            self._mode = mode
            if self._mode != "external":
                raise ValueError("mode must be 'external'")
            self._confirm_legacy = False
        self._timeout = timeout
        self._on_request = on_request
        self._store: ApprovalStore = store if store is not None else InMemoryStore()
        # Explicit override: these tool names always gate, even if classify() says safe.
        self._dangerous_tools = set(dangerous_tools or [])

    # --- Web-UI / async-approval primitives (Req. 4 & 5) ----------------------

    def pending(self) -> List[ApprovalRequest]:
        """Snapshot of requests currently awaiting a decision.

        A frontend polls this, renders the list, and calls :meth:`resolve`.
        Returns frozen ``ApprovalRequest`` objects (safe to serialize).
        """
        return self._store.list_pending()

    def gate(self, func: Callable) -> Callable:
        """Decorator: force ``func`` to always require approval, through THIS
        guard's configured mode (default-terminal / callback / external) — not
        hardcoded terminal. Registers the function's name so it always gates,
        then routes each call through :meth:`execute`. Returns ``ActionDenied``
        on denial, like :meth:`execute`.

            guard = AgentGuard(mode="external", on_request=push)
            @guard.gate
            def charge_card(amount): ...     # parks a UI approval, not a y/n prompt
        """
        tool = getattr(func, "__name__", "function")
        self._dangerous_tools.add(tool)        # always gate this name
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            params = {"args": list(args), "kwargs": kwargs}
            return self.execute(tool, params,
                                executor=lambda _n, _p: func(*args, **kwargs))
        return wrapper

    def resolve(self, request_id: str, approved: bool, *,
                actor: Optional[str] = None) -> ResolveOutcome:
        """Answer one parked request by id, from ANY transport (HTTP, WS,
        webhook). Thread-safe and idempotent: the FIRST call to a given id wins
        and returns ``RESOLVED``; later calls return ``ALREADY_RESOLVED`` (or
        ``EXPIRED`` / ``UNKNOWN``). A double-clicked button or a retried webhook
        is therefore a safe no-op, never a double-execution.

        Backward compatible — only ``RESOLVED`` is truthy, so existing
        ``if guard.resolve(...):`` checks still work.

        ``actor`` (optional) is recorded in the audit log for this decision.
        """
        outcome = self._store.decide(request_id, bool(approved), actor)
        if outcome is ResolveOutcome.RESOLVED:
            record = self._store.get(request_id)
            if record is not None:
                record.event.set()        # wake the one parked execute()/aexecute()
        return outcome

    # --- Registry internals ---------------------------------------------------

    def _fire_on_request(self, request: ApprovalRequest) -> None:
        """Push a freshly-parked request to an external system. Best-effort:
        any failure is logged and swallowed so a broken/​slow notifier can NEVER
        wedge the gate — the request still sits in :meth:`pending` and still
        fails closed on timeout exactly as without a notifier (Req. 2)."""
        if self._on_request is None:
            return
        try:
            self._on_request(request)
        except Exception as exc:
            _log(request.tool, request.params, "ON_REQUEST_ERROR",
                 agent_id=request.agent_id, session_id=request.session_id,
                 request_id=request.request_id, reason=repr(exc))

    def _new_record(self, request: ApprovalRequest) -> _Pending:
        """Create the in-flight record and park it in the store. The expiry
        deadline is set only in external mode (terminal/callback resolve inline
        and are unaffected, preserving their exact existing behavior)."""
        deadline_monotonic = None
        if self._mode == "external" and self._timeout is not None:
            deadline_monotonic = time.monotonic() + self._timeout
        record = _Pending(request, deadline_monotonic)
        self._store.put(record)
        return record

    def _build_request(self, tool: str, params: Any, risk: str,
                       reason: Optional[str], agent_id: Optional[str],
                       session_id: Optional[str]) -> ApprovalRequest:
        deadline = None
        if self._mode == "external" and self._timeout is not None:
            deadline = (datetime.now(timezone.utc)
                        + timedelta(seconds=self._timeout)).isoformat()
        return ApprovalRequest(
            request_id=str(uuid.uuid4()),
            tool=str(tool),
            params=params,
            risk=risk,
            reason=reason,
            agent_id=agent_id,
            session_id=session_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            deadline=deadline,
        )

    def _invoke_confirm(self, request: ApprovalRequest) -> Any:
        if self._confirm_legacy:
            return self._confirm(request.tool, request.params)
        return self._confirm(request)

    # --- Decision flow --------------------------------------------------------
    # Every mode funnels into one model: park the request in the store, let a
    # decision arrive via resolve(), wait on this request's own event, read its
    # slot. Only the external branch changed for this release — terminal and
    # callback still resolve inline exactly as before.

    def _decide_sync(self, request: ApprovalRequest) -> Tuple[bool, str, Optional[str]]:
        record = self._new_record(request)
        try:
            if self._confirm is not None:                  # callback (incl. default terminal confirm)
                answer = False
                try:
                    result = self._invoke_confirm(request)
                    if inspect.iscoroutine(result):
                        result.close()      # async confirm needs aexecute()
                    else:
                        answer = bool(result)
                except Exception:
                    answer = False           # broken approver -> fail closed
                self.resolve(request.request_id, answer)
            else:                                          # external mode
                self._fire_on_request(request)             # push; never blocks the gate

            got = record.event.wait(self._timeout)
            if not got:
                return False, "approval_timeout", None
            approved = record.decision is True
            return approved, ("approved" if approved else "user_denied"), record.actor
        finally:
            # Terminal/callback resolve inline and need no late idempotent
            # resolve(); external retains its record so resolve() stays
            # idempotent and can report ALREADY_RESOLVED / EXPIRED.
            if self._mode != "external":
                self._store.remove(request.request_id)

    async def _decide_async(self, request: ApprovalRequest) -> Tuple[bool, str, Optional[str]]:
        record = self._new_record(request)
        loop = asyncio.get_running_loop()
        try:
            if self._confirm is not None:                  # callback (incl. default terminal confirm)
                answer = False
                try:
                    result = self._invoke_confirm(request)
                    if inspect.iscoroutine(result):
                        result = await result
                    answer = bool(result)
                except Exception:
                    answer = False
                self.resolve(request.request_id, answer)
            else:                                          # external mode
                self._fire_on_request(request)

            # Wait without blocking the loop: park the threading.Event in the
            # default executor. resolve() (called from anywhere) wakes it.
            got = await loop.run_in_executor(
                None, record.event.wait, self._timeout)
            if not got:
                return False, "approval_timeout", None
            approved = record.decision is True
            return approved, ("approved" if approved else "user_denied"), record.actor
        finally:
            if self._mode != "external":
                self._store.remove(request.request_id)

    # --- Public entry points --------------------------------------------------

    def execute(self, tool_name: str, params: Any = None, executor: Callable = None,
                *, reason: Optional[str] = None, agent_id: Optional[str] = None,
                session_id: Optional[str] = None) -> Any:
        if not callable(executor):
            raise ValueError("guard.execute requires executor=<callable>")
        risk = classify(tool_name)
        if risk is None and tool_name in self._dangerous_tools:
            risk = "MANUALLY FLAGGED - always requires approval"
        if risk is None:                       # safe tool: silent, zero friction
            return executor(tool_name, params)
        request = self._build_request(
            tool_name, params, risk, reason, agent_id, session_id)
        approved, why, actor = self._decide_sync(request)
        if approved:
            _log(tool_name, params, "APPROVED", reason=reason, agent_id=agent_id,
                 session_id=session_id, request_id=request.request_id, actor=actor)
            return executor(tool_name, params)
        _log(tool_name, params, "BLOCKED", reason=why, agent_id=agent_id,
             session_id=session_id, request_id=request.request_id, actor=actor)
        return ActionDenied(reason=why, tool=str(tool_name),
                            request_id=request.request_id)

    async def aexecute(self, tool_name: str, params: Any = None,
                       executor: Callable = None, *, reason: Optional[str] = None,
                       agent_id: Optional[str] = None,
                       session_id: Optional[str] = None) -> Any:
        if not callable(executor):
            raise ValueError("guard.aexecute requires executor=<callable>")
        risk = classify(tool_name)
        if risk is None and tool_name in self._dangerous_tools:
            risk = "MANUALLY FLAGGED - always requires approval"
        if risk is None:
            return await _maybe_await(executor(tool_name, params))
        request = self._build_request(
            tool_name, params, risk, reason, agent_id, session_id)
        approved, why, actor = await self._decide_async(request)
        if approved:
            _log(tool_name, params, "APPROVED", reason=reason, agent_id=agent_id,
                 session_id=session_id, request_id=request.request_id, actor=actor)
            return await _maybe_await(executor(tool_name, params))
        _log(tool_name, params, "BLOCKED", reason=why, agent_id=agent_id,
             session_id=session_id, request_id=request.request_id, actor=actor)
        return ActionDenied(reason=why, tool=str(tool_name),
                            request_id=request.request_id)


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is a coroutine, else return it as-is."""
    if inspect.iscoroutine(value):
        return await value
    return value
