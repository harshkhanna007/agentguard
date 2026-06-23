"""Proof that the gate fires on every path. Run: python test_agentguard.py"""

import builtins
import json
import os
import tempfile

# Route the audit log to a temp file before importing the package.
_LOG = os.path.join(tempfile.gettempdir(), "agentguard_test.log")
open(_LOG, "w").close()
os.environ["AGENTGUARD_LOG"] = _LOG

import agentguard.core as core
core.LOG_FILE = _LOG  # honor override even though import already happened

from agentguard import AgentGuard, classify, ActionDenied

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond):
    results.append((name, cond))
    print(f"  [{PASS if cond else FAIL}] {name}")


def fake_input(answer):
    """Replace builtins.input with a canned answer."""
    builtins.input = lambda *_a, **_k: answer


# A stand-in tool dispatcher the agent would normally call directly.
def run_tool(name, params):
    return f"EXECUTED {name} {params}"


print("\nDetection:")
check("delete_user is dangerous", classify("delete_user") is not None)
check("DELETE-RECORD is dangerous", classify("DELETE-RECORD") is not None)
check("bulk_delete is dangerous", classify("bulk_delete") is not None)
check("send_email is dangerous", classify("send_email") is not None)
check("run_command is dangerous", classify("run_command") is not None)
check("read_database is SAFE", classify("read_database") is None)
check("get_user is SAFE", classify("get_user") is None)

print("\nSafe tool — silent pass-through (no approval asked):")
fake_input("n")  # would block IF asked; safe tool must not ask
g = AgentGuard()
r = g.execute("read_database", {"id": 1}, executor=run_tool)
check("safe tool executed without prompting", r == "EXECUTED read_database {'id': 1}")

print("\nDangerous tool — terminal approve:")
fake_input("y")
r = g.execute("delete_user", {"id": 2}, executor=run_tool)
check("approved -> executed", r == "EXECUTED delete_user {'id': 2}")

print("\nDangerous tool — terminal deny:")
fake_input("n")
r = g.execute("delete_user", {"id": 3}, executor=run_tool)
check("denied -> ActionDenied(user_denied)",
      isinstance(r, ActionDenied) and r.reason == "user_denied")

print("\nCallback mode (production):")
gc = AgentGuard(confirm=lambda t, p: False)
r = gc.execute("wipe_database", {}, executor=run_tool)
check("callback False -> blocked", isinstance(r, ActionDenied))
gc2 = AgentGuard(confirm=lambda t, p: True)
r = gc2.execute("drop_table", {"t": "users"}, executor=run_tool)
check("callback True -> executed", r.startswith("EXECUTED drop_table"))

print("\nFail-closed on broken callback:")
gb = AgentGuard(confirm=lambda t, p: 1 / 0)
r = gb.execute("delete_everything", {}, executor=run_tool)
check("exploding callback -> blocked (fail-closed)", isinstance(r, ActionDenied))

print("\nguard.gate decorator:")
fake_input("n")
gg = AgentGuard()  # default terminal confirm

@gg.gate
def delete_record(record_id):
    return f"DELETED {record_id}"

r = delete_record(99)
check("decorated fn denied -> blocked", isinstance(r, ActionDenied))
fake_input("y")
check("decorated fn approved -> runs", delete_record(99) == "DELETED 99")
check("guard.gate preserves __name__", delete_record.__name__ == "delete_record")

print("\nMissing executor is rejected:")
try:
    AgentGuard().execute("delete_user", {})
    check("raises without executor", False)
except ValueError:
    check("raises without executor", True)

print("\nAudit log:")
with open(_LOG, encoding="utf-8") as fh:
    rows = [json.loads(line) for line in fh if line.strip()]
decisions = [r["decision"] for r in rows]
check("log has APPROVED entries", "APPROVED" in decisions)
check("log has BLOCKED entries", "BLOCKED" in decisions)
check("safe tool NOT logged", all(r["tool"] != "read_database" for r in rows))
check("every row has ts/tool/params/decision",
      all(all(k in r for k in ("ts", "tool", "params", "decision")) for r in rows))

print("\n" + "=" * 50)
failed = [n for n, ok in results if not ok]
if failed:
    print(f"  {len(failed)} FAILED: {failed}")
    raise SystemExit(1)
print(f"  ALL {len(results)} CHECKS PASSED")
print("=" * 50)
