"""v0.2 external-mode additions. Run: python test_external_resolve.py

Proves the five required behaviors:
  1. idempotent double-resolve  -> 2nd call ALREADY_RESOLVED, waiter not re-triggered
  2. on_request fires exactly once, at park time
  3. on_request raising still results in correct fail-closed timeout
  4. resolve() on an unknown id  -> UNKNOWN
  5. resolve() on an expired id  -> EXPIRED
Plus: actor recorded in the audit log, deadline surfaced on the request.
"""

import json
import os
import tempfile
import threading
import time

_LOG = os.path.join(tempfile.gettempdir(), "agentguard_external_test.log")
open(_LOG, "w").close()
os.environ["AGENTGUARD_LOG"] = _LOG

import agentguard.core as core
core.LOG_FILE = _LOG

from agentguard import AgentGuard, ActionDenied, ResolveOutcome

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def run_tool(name, params):
    return f"EXECUTED {name} {params}"


def wait_until(pred, timeout=3.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# ── 1. Idempotent double-resolve ──────────────────────────────────────────────
print("\n1) idempotent double-resolve:")
calls = {"n": 0}


def counting_exec(name, params):
    calls["n"] += 1
    return "EXECUTED"


guard = AgentGuard(mode="external")
out = {}
t = threading.Thread(target=lambda: out.update(
    r=guard.execute("delete_user", {"id": 1}, executor=counting_exec, agent_id="A")))
t.start()
wait_until(lambda: len(guard.pending()) == 1)
rid = guard.pending()[0].request_id

o1 = guard.resolve(rid, True)
t.join(timeout=3)
o2 = guard.resolve(rid, True)        # duplicate (double-click / webhook retry)
o3 = guard.resolve(rid, False)       # and a conflicting retry

check("first resolve -> RESOLVED", o1 is ResolveOutcome.RESOLVED)
check("first resolve is truthy (back-compat)", bool(o1) is True)
check("approved action executed once", out["r"] == "EXECUTED" and calls["n"] == 1)
check("second resolve -> ALREADY_RESOLVED", o2 is ResolveOutcome.ALREADY_RESOLVED)
check("third (conflicting) resolve -> ALREADY_RESOLVED", o3 is ResolveOutcome.ALREADY_RESOLVED)
check("duplicates are falsy (back-compat)", (not o2) and (not o3))
check("waiter NOT re-triggered (executor still ran once)", calls["n"] == 1)


# ── 2. on_request fires exactly once, at park time ────────────────────────────
print("\n2) on_request fires once at park:")
seen = []
guard2 = AgentGuard(mode="external", on_request=lambda req: seen.append(req))
out2 = {}
t2 = threading.Thread(target=lambda: out2.update(
    r=guard2.execute("wipe_database", {}, executor=run_tool, agent_id="B")))
t2.start()
wait_until(lambda: len(seen) == 1)
check("on_request fired once by park time", len(seen) == 1)
check("on_request received the parked request",
      seen and seen[0].request_id == guard2.pending()[0].request_id)
guard2.resolve(seen[0].request_id, True)
t2.join(timeout=3)
check("on_request did NOT fire again on resolve", len(seen) == 1)


# ── 3. on_request raising still fails closed on timeout ───────────────────────
print("\n3) on_request raising -> still fail-closed timeout:")


def boom(req):
    raise RuntimeError("notifier is down")


guard3 = AgentGuard(mode="external", on_request=boom, timeout=0.15)
# Runs in the main thread: a raising notifier must not propagate or wedge.
r3 = guard3.execute("delete_record", {"id": 9}, executor=run_tool, agent_id="C")
check("raising on_request did not propagate (we got a return value)", True)
check("still failed closed -> ActionDenied", isinstance(r3, ActionDenied))
check("fail-closed reason is timeout", r3.reason == "approval_timeout")


# ── 4. unknown id -> UNKNOWN ──────────────────────────────────────────────────
print("\n4) resolve() unknown id:")
check("unknown id -> UNKNOWN",
      AgentGuard(mode="external").resolve("nope-not-real", True) is ResolveOutcome.UNKNOWN)


# ── 5. expired id -> EXPIRED ──────────────────────────────────────────────────
print("\n5) resolve() expired id:")
guard5 = AgentGuard(mode="external", timeout=0.1)
r5 = guard5.execute("delete_record", {"id": 5}, executor=run_tool, agent_id="D")
check("unresolved request failed closed", isinstance(r5, ActionDenied))
# The ActionDenied carries the id; resolving it now must report EXPIRED.
check("resolve() after timeout -> EXPIRED",
      guard5.resolve(r5.request_id, True) is ResolveOutcome.EXPIRED)


# ── actor recorded in the audit log (Req. 3) ──────────────────────────────────
print("\n+) actor recorded in audit log:")
guard6 = AgentGuard(mode="external")
out6 = {}
t6 = threading.Thread(target=lambda: out6.update(
    r=guard6.execute("delete_user", {"id": 2}, executor=run_tool, agent_id="E")))
t6.start()
wait_until(lambda: len(guard6.pending()) == 1)
guard6.resolve(guard6.pending()[0].request_id, True, actor="alice@slack")
t6.join(timeout=3)
with open(_LOG, encoding="utf-8") as fh:
    rows = [json.loads(x) for x in fh if x.strip()]
approved_E = [r for r in rows if r.get("agent_id") == "E" and r["decision"] == "APPROVED"]
check("approved log row records the actor",
      approved_E and approved_E[-1].get("actor") == "alice@slack")


# ── deadline surfaced on the request (Req. 4) ─────────────────────────────────
print("\n+) deadline metadata:")
g_dl = AgentGuard(mode="external", timeout=60)
out_dl = {}
t_dl = threading.Thread(target=lambda: out_dl.update(
    r=g_dl.execute("delete_user", {"id": 3}, executor=run_tool)))
t_dl.start()
wait_until(lambda: len(g_dl.pending()) == 1)
req_dl = g_dl.pending()[0]
check("deadline populated when timeout set", isinstance(req_dl.deadline, str) and req_dl.deadline)
check("deadline present in to_dict()", "deadline" in req_dl.to_dict())
g_dl.resolve(req_dl.request_id, False)
t_dl.join(timeout=3)
check("no timeout -> deadline is None",
      AgentGuard(mode="external")._build_request(
          "delete_x", {}, "R", None, None, None).deadline is None)


# ── summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 50)
failed = [n for n, ok in results if not ok]
if failed:
    print(f"  {len(failed)} FAILED: {failed}")
    raise SystemExit(1)
print(f"  ALL {len(results)} CHECKS PASSED")
print("=" * 50)
