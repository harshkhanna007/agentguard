"""Lock the README's classification claims to the actual classifier.

Every tool name shown in README.md as "asks first" must classify as dangerous,
and every name shown as running silently must classify as safe. If someone edits
_RISK_RULES and breaks a documented example, this test fails.

Run: python test_readme_examples.py
"""

from agentguard import classify

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


# --- Names the README shows as REQUIRING approval (classify must be non-None) ---
print("README says these ask for approval:")
check("delete_file gated (smallest example, Risk: IRREVERSIBLE)",
      classify("delete_file") == "IRREVERSIBLE - cannot be undone")
check("update_email gated (full example)", classify("update_email") is not None)
check("delete_customer gated (full example)", classify("delete_customer") is not None)

# README 'How it works' lists these example keywords as dangerous:
print("\nREADME 'How it works' keyword examples (all must be gated):")
for kw in ("delete", "wipe", "drop", "send", "run", "write"):
    check(f"a name containing {kw!r} is gated", classify(f"{kw}_thing") is not None)

# --- Names the README shows as running SILENTLY (classify must be None) ---
print("\nREADME says these run with no prompt:")
check("read_customer is safe (full example)", classify("read_customer") is None)

# --- Honest documented gap: classify is NAME-only, so these stay SILENT.
# The README 'Known limitations' lists exactly these as NOT gated. If a future
# change starts gating them, the docs are now wrong and this should be revisited.
print("\nREADME 'Known limitations' — documented to run UNGATED (name looks safe):")
for n in ("charge_card", "transfer_funds", "grant_admin", "read_secrets"):
    check(f"{n} is NOT auto-gated (matches documented limitation)",
          classify(n) is None)

print("\n" + "=" * 50)
failed = [n for n, ok in results if not ok]
if failed:
    print(f"  {len(failed)} FAILED: {failed}")
    raise SystemExit(1)
print(f"  ALL {len(results)} CHECKS PASSED — README matches classify()")
print("=" * 50)
