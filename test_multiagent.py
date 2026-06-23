"""v0.2 concurrency proof. Run: python test_multiagent.py

Proves, against the real code, the six things the spec requires:
  1. two agents can request approval simultaneously
  2. approvals are routed by request_id
  3. approving request B does not affect request A
  4. async execution works (and does not block the event loop)
  5. terminal prompts are serialized
  6. denial is a returned value, not a retry-triggering exception
"""

import asyncio
import builtins
import os
import tempfile
import threading
import time

# Route the audit log to a temp file before importing the package.
_LOG = os.path.join(tempfile.gettempdir(), "agentguard_multiagent_test.log")
open(_LOG, "w").close()
os.environ["AGENTGUARD_LOG"] = _LOG

import agentguard.core as core
core.LOG_FILE = _LOG

from agentguard import AgentGuard, ActionDenied, ApprovalRequest, ResolveOutcome

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def run_tool(name, params):
    return f"EXECUTED {name} {params}"


def wait_until(predicate, timeout=3.0, interval=0.005):
    """Poll predicate() until true or timeout. Used only by the test harness."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ── 1+2+3: two agents at once, routed by id, B's answer never touches A ────────
print("\n[external mode] two concurrent agents, out-of-order resolution:")

guard = AgentGuard(mode="external")
out = {}


def agent_call(name, target):
    out[name] = guard.execute(
        "delete_user", {"id": target}, executor=run_tool, agent_id=name)


tA = threading.Thread(target=agent_call, args=("AGENT-A", 1))
tB = threading.Thread(target=agent_call, args=("AGENT-B", 2))
tA.start()
tB.start()

# Both must be parked and visible at the same time.
both_pending = wait_until(lambda: len(guard.pending()) == 2)
check("two requests pending simultaneously", both_pending)

by_agent = {r.agent_id: r for r in guard.pending()}
check("each pending request has a unique request_id",
      len({r.request_id for r in guard.pending()}) == 2)
check("requests carry agent identity", set(by_agent) == {"AGENT-A", "AGENT-B"})

# Approve B only. A must stay blocked and its thread must NOT finish.
guard.resolve(by_agent["AGENT-B"].request_id, True)
tB.join(timeout=3)
check("approving B lets B through",
      out.get("AGENT-B") == "EXECUTED delete_user {'id': 2}")
check("A still pending after B approved (B did not unblock A)",
      tA.is_alive() and "AGENT-A" not in out)

# Now deny A independently.
guard.resolve(by_agent["AGENT-A"].request_id, False)
tA.join(timeout=3)
check("denying A blocks only A",
      isinstance(out.get("AGENT-A"), ActionDenied)
      and out["AGENT-A"].reason == "user_denied")

# Resolving an unknown / already-resolved id is a no-op, not a crash.
check("resolve() of already-decided id -> ALREADY_RESOLVED",
      guard.resolve(by_agent["AGENT-A"].request_id, True) is ResolveOutcome.ALREADY_RESOLVED)
check("resolve() of unknown id -> UNKNOWN",
      guard.resolve("does-not-exist", True) is ResolveOutcome.UNKNOWN)


# ── 4: async execution + non-blocking event loop ──────────────────────────────
print("\n[async] aexecute works and does not block the loop:")


async def async_suite():
    # 4a. callback approver, sync + async executors, run concurrently.
    g = AgentGuard(confirm=lambda req: req.params["id"] != 99)  # deny id 99

    async def call(name, target):
        return await g.aexecute("delete_user", {"id": target},
                                executor=run_tool, agent_id=name)

    a, b = await asyncio.gather(call("A", 1), call("B", 99))
    check("async approved -> executed", a == "EXECUTED delete_user {'id': 1}")
    check("async denied -> ActionDenied", isinstance(b, ActionDenied))

    # 4b. async confirm + async executor are both awaited.
    async def aconfirm(req):
        await asyncio.sleep(0)
        return True

    async def aexecutor(name, params):
        await asyncio.sleep(0)
        return f"ASYNC {name} {params}"

    g2 = AgentGuard(confirm=aconfirm)
    r = await g2.aexecute("drop_table", {"t": "users"}, executor=aexecutor)
    check("async confirm + async executor awaited",
          r == "ASYNC drop_table {'t': 'users'}")

    # 4c. external mode: while a request is parked, the SAME loop keeps running.
    g3 = AgentGuard(mode="external")
    task = asyncio.create_task(
        g3.aexecute("delete_user", {"id": 7}, executor=run_tool, agent_id="LOOP"))

    ticks = 0
    while not g3.pending():          # the loop is free to do other work
        ticks += 1
        await asyncio.sleep(0.005)
        if ticks > 200:
            break
    check("event loop kept running while approval pending", ticks > 0)

    g3.resolve(g3.pending()[0].request_id, True)
    done = await asyncio.wait_for(task, timeout=3)
    check("external async request resolves and completes",
          done == "EXECUTED delete_user {'id': 7}")


asyncio.run(async_suite())


# ── 5: terminal prompts are serialized ────────────────────────────────────────
print("\n[terminal] concurrent prompts are serialized (never overlap):")

_concurrency = {"now": 0, "max": 0}
_counter_lock = threading.Lock()
_real_stdout = core.sys.stdout
core.sys.stdout = open(os.devnull, "w", encoding="utf-8")  # silence the boxes


def fake_input(*_a, **_k):
    # If prompts overlapped, two threads would be inside input() at once.
    with _counter_lock:
        _concurrency["now"] += 1
        _concurrency["max"] = max(_concurrency["max"], _concurrency["now"])
    time.sleep(0.03)
    with _counter_lock:
        _concurrency["now"] -= 1
    return "y"


builtins.input = fake_input
tguard = AgentGuard()  # terminal mode
threads = [
    threading.Thread(
        target=lambda i=i: tguard.execute(
            "delete_user", {"id": i}, executor=run_tool, agent_id=f"A{i}"))
    for i in range(6)
]
for t in threads:
    t.start()
for t in threads:
    t.join()

core.sys.stdout.close()
core.sys.stdout = _real_stdout
check("never two terminal prompts active at once",
      _concurrency["max"] == 1)


# ── 6: denial is a value, not a retry-triggering exception ─────────────────────
print("\n[denial semantics] denial does not look like a tool failure:")

calls = {"n": 0}


def counting_exec(name, params):
    calls["n"] += 1
    return "EXECUTED"


denier = AgentGuard(confirm=lambda req: False)
# If this raised, control would jump to except — reaching the asserts proves
# it RETURNED instead.
r = denier.execute("delete_user", {"id": 1}, executor=counting_exec)
check("denial returns ActionDenied (not raised)", isinstance(r, ActionDenied))
check("ActionDenied is NOT an Exception (no except-based retry)",
      not isinstance(r, Exception) and not issubclass(ActionDenied, Exception))
check("ActionDenied is falsy", bool(r) is False)
check("denied executor never ran", calls["n"] == 0)
check("legacy confirm(tool, params) still works",
      isinstance(AgentGuard(confirm=lambda t, p: False)
                 .execute("wipe_db", {}, executor=run_tool), ActionDenied)
      and AgentGuard(confirm=lambda t, p: True)
      .execute("drop_table", {}, executor=run_tool).startswith("EXECUTED"))


# ── summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 50)
failed = [n for n, ok in results if not ok]
if failed:
    print(f"  {len(failed)} FAILED: {failed}")
    raise SystemExit(1)
print(f"  ALL {len(results)} CHECKS PASSED")
print("=" * 50)
