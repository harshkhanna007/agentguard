# AgentGuard

AgentGuard is a stop-and-ask checkpoint for software that can take actions on its own. Some programs don't just return text — they can run real operations like deleting a file, sending an email, or charging a card by calling functions in your code. AgentGuard sits in front of those function calls: when a call looks dangerous, it pauses the program and asks a person to approve or deny it before anything runs. If nobody answers in time, the action is blocked. The check is plain code, so the program cannot skip it by "deciding" to.

No dependencies — pure Python standard library.

## Install

```bash
pip install agentguard-hitl
# or, from a clone of this repo:
pip install -e .
```

## Smallest working example

The main way to use AgentGuard is to wrap your tool calls in `guard.execute()`. It looks at the tool's name and, if the name looks dangerous, asks for approval before running the call.

```python
from agentguard import AgentGuard

guard = AgentGuard()  # approval happens in the terminal

def run_tool(name, params):
    return f"ran {name} with {params}"

# "delete_file" contains "delete", so this asks before running:
result = guard.execute("delete_file", {"path": "notes.txt"}, executor=run_tool)
print(result)
```

Run it and you get a prompt in your terminal:

```
[!] AGENTGUARD: Action requires approval
===================================================
Tool      : delete_file
Parameters: {"path": "notes.txt"}
Risk      : IRREVERSIBLE - cannot be undone
Request   : 3f9c...
===================================================
Allow? (y/n):
```

Type `y` and `run_tool` runs. Type `n` — or run it where there's no keyboard — and the call is blocked and `result` is an `ActionDenied` value instead.

## How it works

**What gets gated.** When you call `guard.execute(name, params, executor=run_tool)`, AgentGuard looks at the tool's *name*. If the name contains a word like `delete`, `wipe`, `drop`, `send`, `run`, or `write`, it asks for approval first. If the name looks safe, the call runs right away with no prompt. The check reads only the name — not what the function does or what arguments it gets.

**The two ways to gate, side by side.** `guard.execute()` is the main path: it runs the automatic name check. `guard.gate` is a manual override — a decorator bound to a guard that makes a function *always* require approval, regardless of its name. Both go through whichever approval mode the guard is configured with (terminal / callback / external). Use `execute()` for normal integration; use `guard.gate` to force approval on one specific function you already know is dangerous.

**Where the human is asked.** You pick one when you create the guard:

- **Terminal** (default): a `y/n` question in the console.
- **Callback**: you give a function that returns `True` or `False` (for example, it posts to Slack and waits for a click).
- **External**: the request is parked with an ID, and your own app approves it later by calling `resolve()`; `pending()` lists everything waiting. This is how you connect approvals to a web page or dashboard.

**The result.** If the call is approved, it runs and you get its normal return value. If it is denied — or if no one answers before the optional timeout — it does not run, and you get an `ActionDenied` value back (a value, not an error you have to catch).

## The `guard.gate` decorator

Use `guard.gate` when you know a specific function is always dangerous regardless of its name. It is a decorator **bound to a guard**, so it forces approval through that guard's configured mode — terminal, callback, or external/UI.

```python
guard = AgentGuard()                       # or mode="external", confirm=..., etc.

@guard.gate
def delete_file(path):
    print(f"deleting {path}")

delete_file("notes.txt")   # always asks for approval, through the guard's mode
```

`guard.gate` registers the function's name so it always gates, then routes each call through `guard.execute()`. It gates every call regardless of the name and returns an `ActionDenied` value on denial (the same as `execute()`). Because it uses the guard's mode, on a `mode="external"` guard it shows up as a UI approval card, not a terminal prompt.

## Function reference

Everything exported from the top-level `agentguard` package:

| Name | What it does | Returns |
|---|---|---|
| `AgentGuard` | The checkpoint object you put in front of tool calls. The main entry point. Its constructor takes `dangerous_tools=[...]` — a list of tool names that always require approval regardless of their name, through whichever mode you configured. | an `AgentGuard` instance |
| `classify` | Checks whether a tool name looks dangerous. | a risk-label string, or `None` if safe |
| `ApprovalRequest` | The details of one pending approval (tool, params, risk, ids, timestamp, deadline). Read-only. | an object; `.to_dict()` gives a plain dict |
| `ActionDenied` | The value returned when an action is blocked. It is "falsy" and is a value, not an exception. | an object; `.reason`, `.tool`, `.request_id` |
| `ResolveOutcome` | The result of answering a parked request: `RESOLVED`, `ALREADY_RESOLVED`, `UNKNOWN`, or `EXPIRED`. | an enum member |
| `ApprovalStore` | The interface for plugging in your own storage for pending approvals. | (a type to implement) |
| `InMemoryStore` | The default storage for pending approvals (kept in memory). | an `InMemoryStore` instance |

Methods on an `AgentGuard` instance:

| Method | What it does | Returns |
|---|---|---|
| `execute(tool_name, params, executor, *, reason=None, agent_id=None, session_id=None)` | Gates one tool call. Runs `executor(tool_name, params)` only if allowed. The main integration point. | the executor's result, or `ActionDenied` |
| `aexecute(...)` | Same as `execute`, for `async`/`await` code. | the executor's result, or `ActionDenied` |
| `resolve(request_id, approved, *, actor=None)` | Approves or denies a parked request (external mode). First answer wins. | a `ResolveOutcome` |
| `pending()` | Lists the approval requests currently waiting. | a list of `ApprovalRequest` |
| `gate(func)` | Decorator bound to this guard: forces `func` to always require approval, through this guard's mode. | the wrapped function |

## Full integration example

This is a complete, runnable program. It shows the recommended pattern: keep your real tools as normal functions, put them behind one dispatcher, and route that dispatcher through `guard.execute()`.

```python
from agentguard import AgentGuard, ActionDenied

# 1. Your real tool implementations. Each takes a params dict and does its work.
def read_customer(params):
    return {"id": params["id"], "name": "Jane Doe", "plan": "pro"}

def update_email(params):
    # pretend this writes to a database
    return f"Email for #{params['id']} set to {params['email']}"

def delete_customer(params):
    # pretend this deletes a row
    return f"Customer #{params['id']} deleted"

# 2. One dispatcher mapping a tool name to its implementation.
TOOLS = {
    "read_customer":   read_customer,
    "update_email":    update_email,
    "delete_customer": delete_customer,
}

def run_tool(name, params):
    return TOOLS[name](params)

# 3. Create the guard. Default asks in the terminal.
guard = AgentGuard()
# Other options (pick ONE when you create the guard):
#   guard = AgentGuard(confirm=lambda req: ask_slack_and_wait(req))          # returns True/False
#   guard = AgentGuard(mode="external", on_request=push_to_ui, timeout=120)  # web UI

# 4. Anywhere your code currently runs a tool, route it through the guard instead.
def handle_tool_call(name, params):
    result = guard.execute(
        name, params,
        executor=run_tool,         # the guard calls this only after approval
        agent_id="support-bot",    # optional: who is acting
        session_id="ticket-4821",  # optional: which run this belongs to
    )
    if isinstance(result, ActionDenied):
        # Blocked. Return a plain message; do NOT automatically retry.
        return f"Action blocked: {result.reason}"
    return result

# 5. Use it.
print(handle_tool_call("read_customer", {"id": 7}))                      # safe name -> runs, no prompt
print(handle_tool_call("update_email", {"id": 7, "email": "a@b.com"}))   # "email"/"update" -> asks first
print(handle_tool_call("delete_customer", {"id": 7}))                    # "delete" -> asks first
```

Notes for adapting this:
- `executor` must be a function with the signature `executor(tool_name, params)`.
- A safe-named tool (`read_customer`) runs with no prompt. A dangerous-named one (`update_email`, `delete_customer`) asks first.
- To approve through a web UI instead of the terminal, create the guard with `mode="external"`, push each request to your UI from `on_request`, and call `guard.resolve(request_id, approved, actor=...)` from your Approve/Deny buttons. Always pass a finite `timeout`.
- `guard.gate` is not used here on purpose: `execute()` is the direct path. Reach for `@guard.gate` only to force approval on one specific function — it routes through this same guard, so it uses the same approval mode.

## Use with Cursor, Claude Code, or any AI coding agent

Copy the block below and paste it into your own coding agent. It is a prompt to hand to a tool — not code to run directly.
I want to integrate AgentGuard into this project. AgentGuard is a library 
that pauses an AI agent before it runs a dangerous or irreversible tool call 
and asks a human to approve or deny it first. Install it with:
pip install agentguard-hitl

Before writing a single line of code, do the following discovery steps and 
tell me what you find:

DISCOVERY (do this first, do not skip):
1. Find every place in this codebase where an AI agent executes a tool or 
   function call — the dispatcher, the tool runner, wherever "the agent 
   picked a tool and now it runs." List every file and line.
2. List every tool/function the agent can call. For each one, tell me: 
   does its name sound dangerous (delete, send, wipe, drop, write, run, 
   exec, update) or does it sound innocent but could be dangerous in 
   practice (charge_card, grant_admin, transfer_funds, deploy, 
   read_secrets, export, disable, reset, create_api_key, revoke)?
3. Find where the existing UI is built — what framework (React, Vue, plain 
   HTML, Jinja, etc.), what components already exist for modals, cards, 
   dialogs, notifications, or alerts, and what the existing color scheme, 
   font, spacing, and button styles look like. I want the approval card to 
   look like it belongs in this UI, not like it was dropped in from outside.
4. Find the web framework being used (Flask, FastAPI, Django, Express, etc.) 
   and where routes/endpoints are defined.
5. Find how real-time or live updates currently work in this project — 
   WebSockets, Server-Sent Events, polling, a message queue, or nothing yet. 
   If nothing exists, identify the simplest option that fits this stack.
6. Find if there is an existing authenticated user session — how is the 
   current user identified (user.id, session["user"], request.user, JWT, 
   etc.)? I need this for the approver identity record.

Do not proceed until you have listed findings for all six points above and 
I have confirmed them.

IMPLEMENTATION (after discovery is confirmed):

Follow these exact steps in this exact order. Show me the diff for each 
step before moving to the next. Do not batch them.

STEP 1 — Create the guard (one place, app startup):
- Import AgentGuard and ActionDenied at the top of the appropriate file 
  (wherever app-level objects like db connections or config are initialized).
- Define a push_to_ui(request) function that sends the approval request to 
  the frontend using whatever real-time mechanism exists (or the simplest 
  one you identified). It receives an ApprovalRequest object with these 
  fields: request_id, tool, params, risk, agent_id, session_id, reason, 
  timestamp, deadline. Send all of them — don't drop any.
- Create exactly one guard instance:
  guard = AgentGuard(
      mode="external",
      on_request=push_to_ui,
      timeout=120,
      dangerous_tools=[LIST EVERY INNOCENTLY-NAMED DANGEROUS TOOL YOU FOUND]
  )
- This instance must be importable by both the agent code and the route 
  handlers. Put it somewhere both can reach — a shared module, app state, 
  or dependency injection, whatever fits this project's existing pattern.

STEP 2 — Wrap the tool dispatcher (one line changed):
- Find the exact line(s) from discovery step 1 where tools execute.
- Change each one from:
    result = run_tool(name, params)
  to:
    result = guard.execute(
        name, params,
        executor=run_tool,
        agent_id=<how this agent is identified in this codebase>,
        session_id=<current session or run id if one exists>
    )
- Immediately after, handle denial:
    if isinstance(result, ActionDenied):
        <return or yield the denial message back to the agent in whatever 
        format this project uses for tool results — string, dict, JSON, 
        tool_result block, etc.>
        <do NOT raise an exception, do NOT retry the call>
- If multiple agents share one dispatcher, this one change guards all of 
  them. If each agent has its own, make this change in each one.

STEP 3 — Add two backend routes:
Add these two endpoints using whatever routing pattern this project already 
uses. Match the existing route style exactly (decorators, blueprints, 
routers, controllers — whatever is already here):

Route 1 — list pending approvals (used by the UI to render the queue):
  GET /agentguard/pending
  Returns: guard.pending() serialized as JSON (each ApprovalRequest has 
  a .to_dict() method). Protect this route with whatever authentication 
  middleware already exists on sensitive routes in this project.

Route 2 — resolve an approval (called by Approve/Deny buttons):
  POST /agentguard/resolve
  Body: { request_id: string, approved: boolean }
  Action: guard.resolve(request_id, approved, actor=<current user identity>)
  Returns: { outcome: <ResolveOutcome value> }
  On UNKNOWN or EXPIRED outcome, return an appropriate error response.
  Protect this route identically to Route 1.

STEP 4 — Build the approval UI component:
- Build a single approval card component that matches the existing UI 
  exactly — same framework, same design system, same component library if 
  one exists. Do not introduce a new UI framework or new CSS library.
- The card must show ALL of these fields, labeled clearly:
    Tool name (what the agent wants to run)
    Parameters (what it's passing — shown as a readable key/value list)
    Risk level (the risk label from AgentGuard)
    Agent ID (which agent is asking)
    Session ID (which run this belongs to)
    Reason (if provided)
    Countdown timer to deadline (live, counts down in seconds)
- Two buttons: Approve and Deny. On click, each calls the resolve route 
  with the correct request_id and approved=true/false.
- After clicking either button, disable both immediately to prevent 
  double-submission (the library handles it safely, but the UI should 
  reflect that the decision was made).
- When the deadline countdown hits zero, mark the card as expired and 
  disable both buttons.
- Wire the card to receive new requests via the same real-time mechanism 
  used in push_to_ui (step 1). The card should appear automatically when 
  a new request is parked — no manual refresh.
- Match the existing UI's: color scheme, font sizes, border radius, spacing, 
  button styles (primary/danger), modal or panel patterns, and any existing 
  loading/error states. If there's an existing modal or dialog component, 
  use it as the wrapper. If there are existing button components, use them.

STEP 5 — Verify end to end:
Run through this exact sequence manually and confirm each step works:
1. Trigger an agent action that calls a tool with a dangerous-sounding name.
   Confirm: card appears in UI, agent is paused.
2. Click Approve. Confirm: agent continues, tool runs, result is returned.
3. Trigger the same action again. Click Deny. 
   Confirm: ActionDenied is returned, agent receives the denial message, 
   does not retry.
4. Trigger an action that calls a tool from the dangerous_tools list 
   (innocently named). Confirm: card appears (it was not silently allowed).
5. Trigger an action that calls a safe tool (not dangerous-named, not in 
   dangerous_tools list). Confirm: no card appears, tool runs immediately.
6. Click Approve twice on the same card (simulate double-click). 
   Confirm: second click returns ALREADY_RESOLVED, nothing bad happens.

THINGS YOU MUST NOT DO:
- Do not use @gate (the old top-level decorator — it no longer exists).
  Use @guard.gate if you need to force-gate one specific function.
- Do not create more than one AgentGuard instance.
- Do not modify AgentGuard's internal code (core.py).
- Do not auto-retry a denied action.
- Do not set timeout=None — always use a finite number.
- Do not run the agent process and the web server in separate processes 
  unless you flag this to me explicitly, because cross-process approval 
  is not supported out of the box.
- Do not invent a new UI style — match what already exists.

After all five steps are complete and verified, give me:
1. A list of every file changed and exactly what was changed in each.
2. A list of any tools you found that are dangerous but NOT currently 
   covered by either the name classifier or the dangerous_tools list, 
   so I can decide whether to add them.
3. Any architectural concern you noticed — specifically whether the agent 
   and web server run in the same process, and if not, what that means 
   for this integration.
```

## Known limitations

Read this before relying on AgentGuard. These are real and current.

**Critical / high — do not ignore:**

- **Ungated actions are also unlogged.** Only gated actions are written to the audit log. Combined with the point above, a dangerous-but-ordinarily-named action can run with no record at all.
- **Approved parameters are not frozen.** The values shown to the approver are held by reference. If they change between approval and execution, a different action can run than the one that was approved.
- **`confirm` plus `mode="external"` silently becomes callback mode.** If you pass both, the external/UI mode is ignored without warning. If your callback returns `True`, everything is auto-approved.
- **`timeout` defaults to waiting forever, and a wrong type crashes.** With no `timeout`, a parked external-mode request blocks indefinitely if nobody answers. A non-numeric `timeout` raises an error on the first gated call. Always pass a finite number.
- **Async use does not scale.** `aexecute()` parks each wait on a small shared thread pool. Many simultaneous approvals exhaust it and starve other async work, and a slow `on_request` callback blocks the event loop.
- **One process only.** External-mode approval works only when `execute()` and `resolve()` run in the same process. If you split them across processes or containers (agent in a worker, UI in a separate web server), the agent hangs until its timeout even after a human approves. There is no built-in cross-process support.

**Medium / low:**

- **Memory grows over time.** In external mode, resolved requests are kept in memory and never removed; a long-running server slowly accumulates them.
- **Parameters can leak secrets.** Tool parameters are written to the log and sent to the approval UI as-is. There is no redaction.
- **Name matching is fuzzy.** Substring matching over-flags some safe names (for example `evaluate_model`, `prune_old`). The audit log is a single local file with no rotation and no protection against multiple processes writing it at once.

**What you can honestly say it is:** a single-process, human-in-the-loop approval gate for AI tool calls, with terminal, callback, and same-process web-UI approval modes. It blocks when no one answers and when a timeout passes. Within one process, simultaneous approvals don't get mixed up and repeat approvals are safe.

**What it is not (yet):** not a security or authorization system, and not proven for production. It does not work across processes or containers, does not scale for heavy async use, does not keep a complete record of all activity, and does not catch dangerous actions by what they do — only by what they are named.

## License

MIT.

## Contributing

Issues and pull requests are welcome. If you change gating behavior, include a test that covers it (`python test_agentguard.py`, `python test_multiagent.py`, `python test_external_resolve.py`).
