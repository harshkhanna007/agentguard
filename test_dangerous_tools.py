"""Tests for the dangerous_tools explicit override. Run: python test_dangerous_tools.py"""

import os
import json
import tempfile
import threading
import time

_LOG = os.path.join(tempfile.gettempdir(), "ag_dangerous_test.log")
open(_LOG, "w").close()
os.environ["AGENTGUARD_LOG"] = _LOG
import agentguard.core as core
core.LOG_FILE = _LOG

from agentguard import AgentGuard, ActionDenied, classify

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def run_tool(name, params):
    return f"RAN {name}"


class Recorder:
    """A confirm callback that records every request it's asked about."""
    def __init__(self, answer):
        self.calls = []
        self.answer = answer
    def __call__(self, req):
        self.calls.append(req)
        return self.answer


MANUAL = "MANUALLY FLAGGED - always requires approval"

# ── 1. innocent name IN dangerous_tools is gated (not silent) ──────────────────
print("1) innocent-named tool listed in dangerous_tools is gated:")
rec = Recorder(True)
g = AgentGuard(confirm=rec, dangerous_tools=["charge_card"])
r = g.execute("charge_card", {"amt": 100}, executor=run_tool)
check("classify() alone says charge_card is safe", classify("charge_card") is None)
check("but it WAS gated (confirm called)", len(rec.calls) == 1)
check("approved -> executor ran", r == "RAN charge_card")
check("risk label on the request is MANUAL", rec.calls and rec.calls[0].risk == MANUAL)

# ── 2. innocent name NOT in dangerous_tools still runs silently ────────────────
print("\n2) innocent-named tool NOT listed still runs silently:")
rec = Recorder(True)
g = AgentGuard(confirm=rec, dangerous_tools=["charge_card"])
r = g.execute("transfer_funds", {}, executor=run_tool)   # not in the list
check("not gated (confirm NOT called)", len(rec.calls) == 0)
check("ran silently", r == "RAN transfer_funds")

# ── 3. tool in dangerous_tools that ALSO matches classify() gates ONCE ─────────
print("\n3) tool both listed AND matched by classify() -> gated once, not twice:")
open(_LOG, "w").close()
rec = Recorder(False)   # deny, so we can inspect the single block
g = AgentGuard(confirm=rec, dangerous_tools=["delete_record"])
r = g.execute("delete_record", {"id": 1}, executor=run_tool)
check("classify() already catches delete_record", classify("delete_record") is not None)
check("confirm called exactly once", len(rec.calls) == 1)
check("risk = classify()'s label, NOT the manual one (no stacking)",
      rec.calls and rec.calls[0].risk == "IRREVERSIBLE - cannot be undone")
rows = [json.loads(x) for x in open(_LOG, encoding="utf-8") if x.strip()]
check("logged exactly once (no double log)",
      len([x for x in rows if x["tool"] == "delete_record"]) == 1)
check("denied -> ActionDenied", isinstance(r, ActionDenied))

# ── 4. dangerous_tools=None and [] behave identically to no override ───────────
print("\n4) dangerous_tools=None and [] both leave existing behavior unchanged:")
for label, dt in [("None", None), ("[]", [])]:
    rec = Recorder(True)
    g = AgentGuard(confirm=rec, dangerous_tools=dt)
    r = g.execute("read_data", {}, executor=run_tool)   # safe name
    check(f"dangerous_tools={label}: safe tool runs silently, not gated",
          len(rec.calls) == 0 and r == "RAN read_data")

# ── 5. manual label flows into the ApprovalRequest; action is audited (external)
print("\n5) external mode: manual label on the request + gated action audited:")
open(_LOG, "w").close()
g = AgentGuard(mode="external", timeout=5, dangerous_tools=["grant_admin"])
out = {}
t = threading.Thread(target=lambda: out.update(
    r=g.execute("grant_admin", {"uid": 7}, executor=run_tool, agent_id="A")))
t.start()
time.sleep(0.4)
pend = g.pending()
check("shows up as a pending approval (UI card)", len(pend) == 1)
check("ApprovalRequest.risk == manual label", pend and pend[0].risk == MANUAL)
g.resolve(pend[0].request_id, True, actor="ops")
t.join(timeout=3)
check("approved via external resolve() -> executor ran", out.get("r") == "RAN grant_admin")
rows = [json.loads(x) for x in open(_LOG, encoding="utf-8") if x.strip()]
ga = [x for x in rows if x["tool"] == "grant_admin" and x["decision"] == "APPROVED"]
check("gated action recorded in the audit log", len(ga) == 1)
check("audit entry carries the actor", ga and ga[0].get("actor") == "ops")

# ── 6. guard.gate on an external-mode guard routes to resolve(), NOT input() ───
print("\n6) guard.gate on an external-mode guard routes to resolve(), never input():")
import builtins
_real_input = builtins.input


def _boom(*_a, **_k):
    raise AssertionError("input() must not be called in external mode")


builtins.input = _boom
try:
    eg = AgentGuard(mode="external", timeout=5)

    @eg.gate
    def charge_card(amount):
        return f"charged {amount}"

    out = {}
    t = threading.Thread(target=lambda: out.update(r=charge_card(500)))
    t.start()
    time.sleep(0.4)
    pend = eg.pending()
    check("gated @guard.gate call parked for external approval", len(pend) == 1)
    check("not executed yet (waiting on resolve, not a prompt)", "r" not in out)
    eg.resolve(pend[0].request_id, True, actor="ops")
    t.join(timeout=3)
    check("approved via resolve() -> function ran (input never called)",
          out.get("r") == "charged 500")
finally:
    builtins.input = _real_input

# ── summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 50)
failed = [n for n, ok in results if not ok]
if failed:
    print(f"  {len(failed)} FAILED: {failed}")
    raise SystemExit(1)
print(f"  ALL {len(results)} CHECKS PASSED")
print("=" * 50)
