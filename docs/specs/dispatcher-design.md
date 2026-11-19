# Dispatcher

---

## Essence

The Dispatcher is Cairn's client-side executor. It is responsible for:

1. Coordinating the project container lifecycle
2. Dispatching well-defined tasks to Agents
3. Calling the Cairn API on behalf of Agents to write results back to the graph

Agents do not claim Intents directly, do not send heartbeats directly, and do not call the Cairn API directly. An Agent only receives tasks dispatched by the Dispatcher and returns structured results; the Dispatcher then decides how to request the Server.

---

## Design Highlights

1. Agent output tasks converge into three types: `bootstrap`, `reason`, and `explore`. `bootstrap` runs only in the project's initial state, letting the Agent attempt to solve the whole problem directly; the main phase returns only when the problem is solved, and it must include both the key Fact and `complete`; if the main phase times out, `bootstrap_conclude` wraps up and produces a Fact. `reason` reads the graph to judge whether it is complete or whether a new intent should be proposed; `explore` executes one claimed intent and produces one Fact conclusion.
2. The Dispatcher is the sole protocol writer and control plane; Agents do not claim, do not heartbeat, and do not call the Cairn API directly.
3. Timeout policy is defined per task type; both `bootstrap` and `explore` support a two-phase mode of "first-phase execution + on timeout / parse-fail, enter conclude with the same session for wrap-up".
4. Prompts are distributed with the code as markdown files; switching between prompt groups is supported; Worker behavior is implemented by drivers such as `claudecode`, `codex`, and `mock`; `dispatch.yaml` only describes runtime parameters.
5. For scheduling: in the initial state, whether to prefer `bootstrap` or go straight to `reason` is decided by `project.bootstrap_enabled` and Worker capability; in non-initial states, when new developments such as new Fact / Hint appear, `reason` takes priority, otherwise claimable `explore` intents are consumed first. The concurrency constraint of `reason` is expressed as "at most one per project" through the server-side project-level `project.reason` lease; the concurrency constraint of `bootstrap` is "at most one reserved bootstrap intent and at most one bootstrap task per project"; parallelism across projects is allowed. `runtime.interval` is deliberately reused as both the main loop cadence and the heartbeat interval of claim-carrying tasks.
6. A Worker is modeled as an independent LLM concurrency quota unit; the same key is never split across multiple Workers, so concurrency control simply uses `workers[].max_running`.
7. Runtime logs are designed around "state changes first": steady polling, normal heartbeats, and repeated skips should not spam the log; events such as container creation, task dispatch, new process start inside a container, health checks, timeouts, wrap-ups, intent releases, and a worker entering a short unselectable window must be visible.
8. Project container wrap-up must not block the main scheduling loop; container cleanup for multiple completed projects can run in parallel.
9. Once a project leaves `active`, the Dispatcher must treat it as a hard stop: no new tasks are dispatched; locally running `bootstrap`, `explore`, and `reason` tasks are cancelled immediately; cancelled tasks no longer enter the conclude fallback; and on subsequent polls the project container is stopped and any Agent processes still running inside it are killed.
10. The current implementation is designed and tested for a "single Dispatcher instance"; multiple Dispatchers connected to the same server scheduling together is not supported.
11. If a project once `completed` is explicitly `reopen`ed to `active` by the server, the Dispatcher takes no special branch: it treats the project as an ordinary active project and keeps scheduling; and if the project container is still queued for cleanup, the Dispatcher waits for that cleanup to finish first, avoiding races with the old completed/stopped cleanup.
12. Known limitation: the current protocol only records the current claim holder and keeps no worker history for an Intent; therefore, after a project is `stopped` and the open intents' `worker` fields are cleared by the server, neither the Dispatcher nor the UI/API can directly show "who was last driving this intent before the stop". To add this observability later, a reasonable direction is a history field like `worker_history` on Intent, rather than changing the current claim semantics.

Additionally:

- When a project is deleted, the Dispatcher treats it as `deleted`. Like `stopped`, this first cancels locally running tasks, but the container cleanup semantics differ: orphan containers corresponding to `deleted` are removed directly instead of only being stopped and retained.

---

## Architecture Overview

This project can be divided into 4 architectural parts:

1. Cairn Server
2. Dispatcher
3. Project containers
4. Worker / Agent CLI

### 1. Cairn Server

The Server is the source of truth for the protocol.

It is responsible for:

- Storing Projects / Facts / Intents / Hints
- Providing the protocol API
- Maintaining Intent claim, heartbeat, and conclusion state
- Maintaining claim, heartbeat, and release state of the project-level `reason` lease

### 2. Dispatcher

The Dispatcher is the core this project aims to implement.

It is responsible for:

- Pulling project graph state
- Deciding which task type to dispatch now
- Selecting which Worker executes it
- Managing project containers and Worker processes
- Maintaining sessions, timeouts, health checks, and wrap-up
- Writing results back to the Cairn Server

### 3. Project Containers

Each project maps to one running container.

This container is the project's execution environment and is typically responsible for:

- Providing the toolchain
- Providing the network environment
- Hosting the Worker processes of that project

### 4. Worker / Agent CLI

A Worker is not a protocol participant itself, but an execution unit managed by the Dispatcher.

For example:

- Claude Code CLI
- Codex CLI

They are responsible for:

- Receiving the prompt rendered by the Dispatcher
- Executing the task inside the current container
- Producing structured JSON output

### Component Relationships

```text
                         +----------------------+
                         |     Cairn Server     |
                         |----------------------|
                         | Projects / Facts     |
                         | Intents / Hints      |
                         | Protocol API         |
                         +----------^-----------+
                                    |
                           read / write API
                                    |
+-----------------------------------------------------------+
|                         Dispatcher                        |
|-----------------------------------------------------------|
| Scheduling / Task Dispatch / Session / Timeout / Health   |
| Container Lifecycle / Protocol Writeback                  |
+----------------------+----------------------+-------------+
                       |                      |
             manage container        manage container
                       |                      |
          +------------v-----------+  +------v-------------+
          |   Project Container A  |  | Project Container B|
          |------------------------|  |--------------------|
          | Worker / Agent CLI     |  | Worker / Agent CLI |
          | - Claude Code          |  | - Codex            |
          | - Codex                |  | - ...              |
          +------------------------+  +--------------------+
```

### Main Execution Flow

The Dispatcher reads two kinds of data at the same time:

- Structured API data: used for scheduling, state judgment, intent selection, and protocol writeback
- `GET /projects/{project_id}/export?format=yaml`: used only to build the graph snapshot needed for prompts

1. The Dispatcher reads the project graph from the Server
2. The Dispatcher selects the task type and Worker according to the scheduling rules
3. The Dispatcher renders the prompt and command placeholders
4. For `explore`, the Dispatcher first claims the target intent via `POST /projects/{project_id}/intents/{intent_id}/heartbeat`; for `reason`, it first claims the project-level reason lease via `POST /projects/{project_id}/reason/claim`
5. The Dispatcher starts the Worker process inside the project container
6. The Worker outputs structured JSON
7. The Dispatcher parses the result and calls `POST /projects/{project_id}/complete`, `POST /projects/{project_id}/intents`, `POST /projects/{project_id}/intents/{intent_id}/conclude`, `POST /projects/{project_id}/intents/{intent_id}/release`, or `POST /projects/{project_id}/reason/release`

If a project is switched to `stopped`, this main flow short-circuits on the next round: the Dispatcher no longer includes the project in the active scheduling set; it first cancels locally running tasks, then moves into the container cleanup flow and stops the project container. For `bootstrap` / `explore`, being cancelled by `stopped` does not enter the conclude fallback, so no extra Fact is written. After the project resumes to `active`, the Dispatcher re-reads the graph state and then decides whether to continue `explore`, enter `reason`, or — in the initial state — choose `bootstrap` or `reason` again based on `project.bootstrap_enabled` and Worker capability. If a project is `reopen`ed by the server after `completed`, it is equivalent for the Dispatcher to "became active again with one new fact on the graph"; the next round continues scheduling it as an ordinary active project.

Worker selection rules:

1. First filter by task type
2. Then filter out Workers that have reached `max_running`
3. Then filter out unhealthy Workers inside their short unselectable window
4. Among the remaining Workers, prefer the one with the smallest `priority`
5. If `priority` ties, prefer the one with fewer currently running tasks
6. If still tied, choose randomly
7. For `explore`, the Dispatcher first claims successfully via `POST /projects/{project_id}/intents/{intent_id}/heartbeat` before actually starting the task
8. For `reason`, after a successful claim the lease is maintained via `POST /projects/{project_id}/reason/heartbeat`; when `runtime.worker_healthcheck=startup_and_task`, the selected Worker is health-checked once more before actually starting; if that fails, this task is discarded, and the Worker enters a short unselectable window to be retried in later rounds

---

## Configuration Model

The Dispatcher uses one runtime configuration file:

- `dispatch.yaml`

In other words:

- Users only need to provide `dispatch.yaml`
- Task prompts are distributed with the code as markdown files, selected via `runtime.prompt_group`
- Worker health checks, command templates, session handling, and second-phase wrap-up capability are implemented by the corresponding driver
- `runtime.execution` selects the execution backend: `container` (default, one container per project) or `local` (workers run as child processes directly on the dispatcher host, reusing the host's already-configured CLIs — no Docker and no API keys required)

The code directory can be organized like this:

```text
dispatcher/
  models.py
  prompting.py
  output_parser.py
  contracts.py
  prompts/
    default/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
    mock/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
  workers/
    base.py
    registry.py
    adapters/
      claudecode.py
      codex.py
      mock.py
```

The appendix of this document provides:

- Example content of `dispatch.example.yaml`
- Example content of the markdown prompts above

---

## Task Model

### The Three Task Types at a Glance

| Task | Trigger | Input | Output | Timeout policy |
| --- | --- | --- | --- | --- |
| `bootstrap` | Project is `active`; `project.bootstrap_enabled=true`, and either the config contains a Worker that supports `bootstrap` or the project already has a reserved bootstrap intent; facts contain only `origin` and `goal`; there is currently no ordinary intent; either no bootstrap intent exists or only the reserved open `bootstrap` intent exists | `{origin}`, `{goal}`, `{hints}` | On main-phase success, always returns `fact + complete`; the wrap-up phase returns only `fact` | Two-phase: after `timeout` it may enter `conclude_timeout` wrap-up; if both phases fail, the reserved intent is released and the next round retries the project as new |
| `reason` | Project is `active`; the project currently has no unclaimed intent; no other `reason` is running in the project; first trigger or a "new developments" retrigger condition is met | `{graph_yaml}`, `{fact_ids}`, `{open_intents}` | A `complete` object; or an `intent` object; or empty `data` | `timeout` only; timeout or invalid results are discarded outright without writing to the graph |
| `explore` | Project is `active`; there is an unconcluded intent that can be claimed now | `{graph_yaml}`, `{intent_id}`, `{intent_description}` | One Fact conclusion description | Single-phase: timeout discards it outright; two-phase: on timeout or output parse failure it may enter `conclude` wrap-up |

### `bootstrap`

#### Trigger Conditions

- The project is still `active`
- `project.bootstrap_enabled = true`, and either the config contains a Worker that supports `bootstrap` or the project already has a reserved bootstrap intent
- Facts are exactly `origin` and `goal`
- Intents are empty, or only the reserved open `bootstrap` intent exists
- The reserved `bootstrap` intent convention is fixed as:
  - `description = "bootstrap"`
  - `creator = "dispatcher.bootstrap"`
  - `from = ["origin"]`

#### Input

- `{origin}`
- `{goal}`
- `{hints}`

Where:

- `{hints}` is JSON array text, so the Worker can quickly absorb strategy information at project start
- `bootstrap` does not read the graph YAML and does not depend on the ordinary intent graph structure

#### Output Contract

`bootstrap` uses its own output contract:

- The main phase returns only when the problem has been solved
- A main-phase return must contain both `fact` and `complete`
- If the main phase does not solve the problem before the timeout, no valid result is returned; the Dispatcher terminates it and enters `bootstrap_conclude`
- `bootstrap_conclude` is only responsible for wrap-up summarization, so it returns only `fact`

```json
{
  "accepted": true,
  "data": {
    "fact": {
      "description": "Captured two flags, flag{...} and flag{...}; also obtained an admin shell, proof of access in /tmp/proofs/root.txt"
    },
    "complete": {
      "description": "All key results required to complete the Goal have been captured and verified; Goal achieved"
    }
  }
}
```

Constraints:

- On main-phase return, both `data.fact.description` and `data.complete.description` must be present
- The main phase must not return only `fact`
- `bootstrap_conclude` may only return `fact`; it must not return `complete`

#### API Mapping

`bootstrap` reuses the ordinary intent protocol, but with a reserved intent:

| API | Purpose | When used |
| --- | --- | --- |
| `POST /projects/{project_id}/intents` | Create the reserved `bootstrap` intent | When an initial-state project first enters and the intent does not exist yet |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | Claim and maintain the `bootstrap` intent | Claim before dispatching; send periodically at `interval` during execution |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | Write the key result produced by `bootstrap` as a Fact | Called after `bootstrap` or `bootstrap_conclude` returns a valid `fact` |
| `POST /projects/{project_id}/complete` | Complete the project directly based on the just-written bootstrap fact | Called only when the `bootstrap` main phase returns `fact + complete` successfully |
| `POST /projects/{project_id}/intents/{intent_id}/release` | Abandon this bootstrap attempt | Called when both phases fail, or the command fails outright |

#### Timeout and Failure

- The first phase of `bootstrap` uses `timeout`
- The second phase `bootstrap_conclude` uses `conclude_timeout`
- If the main phase solves the problem within `timeout`, it returns `fact + complete`
- If the first phase times out, output parsing fails, or the result does not satisfy the contract, and the Worker supports session / conclude, it enters `bootstrap_conclude`
- The `bootstrap_conclude` prompt must explicitly require "do not keep pushing, do not wait for unfinished tasks, only summarize the most critical facts confirmed so far", so it only produces a `fact`
- If the `bootstrap` main phase returns valid JSON successfully, the Dispatcher first concludes the fact, then immediately completes
- If `bootstrap_conclude` returns valid JSON and the conclude writeback succeeds, the reserved intent is concluded and the project is no longer considered initial-state
- If the main-phase `complete` writeback fails, the already-written fact is retained, and a later `reason` round can still complete the project
- If both phases fail, or the conclude writeback fails, the current `bootstrap` intent is released without writing a Fact; the project is treated as new again next round

### `reason`

#### Trigger Conditions

- The project is still `active`
- The project currently has no unclaimed intent
- The project's `project.reason` is empty, i.e., no other `reason` lease is currently running
- The first trigger only happens when "there are currently no open intents at all"
- Afterwards it is retriggered only when new developments appear; "new developments" here is limited to:
  - Fact count increased
  - Hint count increased
  - The project went from "has open intents" to "has no open intents"
- A single `explore` failure, lost heartbeat, or release with the intent still open does not constitute new developments and must not trigger a new `reason`

#### Input

- `{graph_yaml}`
- `{fact_ids}`
- `{open_intents}`

Where:

- `{fact_ids}` is JSON array text explicitly listing the currently valid Fact ids
- `{open_intents}` is JSON array text explicitly listing all currently unconcluded intents; therefore even if another intent is being `explore`d, `reason` may still be triggered again as long as a new Fact / Hint appeared
- Both placeholders are prompt-level aids and do not replace the server's final validation

#### Output Contract

Completed:

```json
{
  "accepted": true,
  "data": {
    "complete": {
      "from": ["f008"],
      "description": "flag{abc} satisfies the goal requirement"
    }
  }
}
```

Not completed, proposing a new intent:

```json
{
  "accepted": true,
  "data": {
    "intent": {
      "from": ["f003"],
      "description": "Try SQL injection"
    }
  }
}
```

Not completed, no new intent proposed:

```json
{
  "accepted": true,
  "data": {}
}
```

Constraints:

- When `data.complete` is present, it must be an object, and both `complete.from` and `complete.description` must exist
- When `data.complete` is present, it must not also carry `intent`
- If `intent` is present, both `intent.from` and `intent.description` must exist
- If `{open_intents}` is empty, there is no ongoing exploration in the graph; in that case, if there is no `data.complete`, an `intent` must be returned
- If `{open_intents}` is non-empty and there is no `data.complete`, omitting `intent` is allowed

#### API Mapping

Before starting `reason`, the Dispatcher must claim the project-level reason lease and keep heartbeating during execution; this state appears directly in the `project.reason` field of `GET /projects` and `GET /projects/{project_id}` for the frontend and other consumers to observe.

| `reason` output | Dispatcher action | Notes |
| --- | --- | --- |
| `data.complete` present | Call `POST /projects/{project_id}/complete` | `worker` uses the `workers[].name` of the worker executing this task |
| `data.complete` absent, with `intent` | Call `POST /projects/{project_id}/intents` | `creator` uses the current Worker name; `worker` is always written as `null` |
| `data` is an empty object | Do not write to the graph | No Fact / Intent / Complete is written |

Log semantics for writeback failure:

- If `POST /projects/{project_id}/complete` or `POST /projects/{project_id}/intents` returns `403`, it usually means the project is no longer `active`; the task is discarded outright and logged at `info`
- Other write failures are also discarded outright with no immediate retry, only logged
- Whether or not this round writes to the graph, as long as the project is still `active` and the reason lease is still in its own hands, the Dispatcher calls `POST /projects/{project_id}/reason/release` during wrap-up; if the project is already `completed` or `stopped`, the server clears the lease directly

#### Timeout and Failure

- `reason` uses only `timeout`
- Timeout discards it outright
- `accepted: false` discards it outright, logged at `warn`
- Other execution errors also discard it outright, for example:
  - Command exit code is not `0`
  - Output is not valid JSON
  - JSON is missing required fields
- In all these cases, no Fact / Intent / Complete is written; only a log entry

### `explore`

#### Trigger Conditions

- The project is still `active`
- There is an unconcluded intent that can be claimed now

#### Input

- `{graph_yaml}`
- `{intent_id}`
- `{intent_description}`

#### Output Contract

Normal return:

```json
{
  "accepted": true,
  "data": {
    "description": "Found error-based injection in the /search parameter"
  }
}
```

Even when no vulnerability is found, an objective exploration conclusion should be returned rather than an empty response. For example:

```json
{
  "accepted": true,
  "data": {
    "description": "Tested common SQL injection payloads on the /search parameter; no exploitable injection signs found"
  }
}
```

Constraints:

- `data.description` must exist and must be an objective factual conclusion
- Text such as "I refuse to help with penetration" or "this is unsafe" is not allowed as `description`

#### API Mapping

`explore` involves three kinds of protocol APIs:

| API | Purpose | When used |
| --- | --- | --- |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | Claim and maintain ownership | Claim before dispatching; send periodically at `interval` during execution |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | Produce a Fact and conclude the Intent | Called after `execute` or `conclude` returns a valid conclusion |
| `POST /projects/{project_id}/intents/{intent_id}/release` | Abandon the current attempt | Used on failure paths |

The dispatch order is fixed:

1. The Dispatcher first selects a claimable intent
2. The Dispatcher first calls `POST /projects/{project_id}/intents/{intent_id}/heartbeat` once as the claim
3. Only after the heartbeat succeeds is the `explore` Worker actually started

#### Timeout and Failure

`explore` must be compatible with two modes:

1. Single-phase mode: only `execute`
2. Two-phase mode: `execute + session + conclude`

Here `conclude` is an additional wrap-up phase, not a required stage of the main flow.

The first phase uses `timeout`.
In two-phase mode, the second phase uses `conclude_timeout`.

Normal completion:

- If `execute` returns valid JSON within `timeout` with `accepted: true`
- The Dispatcher directly calls `POST /projects/{project_id}/intents/{intent_id}/conclude`
- A successful `POST /projects/{project_id}/intents/{intent_id}/conclude` completes the conclusion; no extra `release` is needed
- If `POST /projects/{project_id}/intents/{intent_id}/conclude` fails to write, the task is discarded outright with no immediate retry, the current intent is released, and only a log entry is written

Anomalies that may enter second-phase wrap-up:

- These anomalies enter `conclude` only on "two-phase Workers"
- Only two kinds of anomalies qualify:
  - Execution timeout
  - The Dispatcher cannot correctly extract and parse the result from the first-phase output, for example:
    - Output is not valid JSON
    - JSON is missing required fields
    - `accepted: true` but the `data` structure does not meet the current task's requirements
- These anomalies do not enter `conclude` on "single-phase Workers"

The two-phase wrap-up flow is fixed:

1. The Dispatcher kills the current process
2. The session id of this task is retained
3. While maintaining the heartbeat, it directly enters `conclude` with the same session
4. The `conclude` prompt must explicitly require "do not keep exploring, only summarize the exploration done so far and its conclusions"
5. If `conclude` returns valid JSON within `conclude_timeout` with `accepted: true`:
   - The Dispatcher calls `POST /projects/{project_id}/intents/{intent_id}/conclude`
   - On success, it is done
6. If `conclude` times out again, the output is invalid, it returns `accepted: false`, or `POST /projects/{project_id}/intents/{intent_id}/conclude` fails to write:
   - The whole exploration is discarded
   - No graph data is written
   - The current intent is released
   - Only a log entry is written

Anomaly handling for single-phase Workers:

- If the current Worker supports neither `session` nor `conclude`, it is in single-phase mode
- In that case, once "timeout" or "output parse / structure validation failure" occurs in the first phase, it is treated as a failure outright
- The handling is: kill the process, discard the whole exploration, write no graph data, release the current intent, log at `warn`

Direct failures that do not enter `conclude`:

- The first phase returns `accepted: false`
- The command exit code is not `0`
- The Worker process produced no readable result at all
- The Dispatcher already determined this execution failed before entering result parsing

In all these cases:

- Do not enter `conclude`
- Write no graph data
- Release the current intent
- Clean up local task state
- Only log

---

## Scheduling Policy

### Global Scheduling

Core rules:

1. Running projects take priority, but only for immediately dispatchable tasks
2. If a running project is in the initial state and can execute `bootstrap`, prefer continuing it
3. Otherwise, if a running project has an executable `explore`, prefer continuing its exploration
4. If all running projects currently have no dispatchable tasks and `runtime.max_running_projects` still has headroom, start one new project that has not started

The design convention of `runtime.interval`:

- `runtime.interval` is not just an ordinary polling interval
- It is deliberately reused as a unified cadence in two places:
  - The Dispatcher main loop interval
  - The heartbeat interval of claim-carrying tasks (`bootstrap` / `explore`)
- The goal is to reduce extra timing parameters and keep the implementation simple for now
- This is an explicit design decision, not accidental coupling

The scheduling pseudocode can stay at this granularity:

```text
for project in running_projects_round_robin:
  if has_dispatchable_bootstrap(project):
    dispatch_bootstrap(project)
    continue
  if has_dispatchable_reason(project):
    dispatch_reason(project)
    continue
  if has_dispatchable_explore(project):
    dispatch_explore(project)
    continue

if running_project_count < runtime.max_running_projects:
  maybe_start_one_new_project()
```

### Per-Project Scheduling

For a single project, after reading the full project state, the Dispatcher schedules in this order:

1. If the project is still in the initial state, first decide the path by `project.bootstrap_enabled` and Worker capability: when it is disabled or no Worker supports `bootstrap`, go straight to `reason`, otherwise execute `bootstrap`; if a reserved bootstrap intent already exists, continue that phase
2. If a "new developments" retrigger condition is met, dispatch `reason` first
3. Otherwise, if there is an unclaimed intent, dispatch `explore`
4. `reason` deduplication is based on "developments", not on total graph changes: the first trigger happens only when there are currently no open intents at all; afterwards it retriggers only when the current Fact / Hint count increased, or the project went from "has open intents" to "has no open intents"
5. If `reason` returns `data.complete`, the Dispatcher calls `POST /projects/{project_id}/complete`
6. If `reason` does not return `data.complete` but carries `intent`, the Dispatcher calls `POST /projects/{project_id}/intents`
7. If `reason` returns neither `data.complete` nor `intent`, nothing is written to the graph this round

Additionally:

- In an initial-state project, if the `bootstrap` path was chosen and the bootstrap intent is already claimed, this round dispatches no `reason` or ordinary `explore`
- Even if an `explore` is already in progress in the same project, dispatching one `reason` task is allowed
- But the precondition is not "an intent was just added" — it is "new developments such as new Fact / Hint genuinely appeared"; merely because the previous `reason` just created a new intent, another `reason` should not fire immediately
- It still requires that there is currently no unclaimed intent, no other `reason` task running in the project, and `runtime.max_project_workers` is not exceeded

Keep the `reason` deduplication rules simple:

- Record the Fact count, Hint count, and whether open intents still existed at the project's last successful `reason`
- The Dispatcher establishes a baseline checkpoint for "active projects that have been observed, already have open intents, but have no checkpoint yet"; this applies both to projects that already exist at startup and to late-arriving running projects, and must not swallow the first new Fact / Hint added during the run
- On the first occurrence with no history and currently no open intents, trigger directly
- Afterwards, retrigger `reason` only when the Fact / Hint count increases, or the project goes from "has open intents" to "no open intents"
- Do not treat "total intent count increased" as a retrigger condition; it usually just means the previous `reason` created a new intent and does not represent new developments
- An `explore` execution failure, lost heartbeat, or temporary release only makes the intent wait to be explored again; it does not trigger an extra `reason`

### Concurrency Constraints

- Under the current design, only one Dispatcher instance may be connected to the same server for scheduling
- Running multiple Dispatchers at the same time is not a supported scenario: locally maintained admission, Worker health state, concurrency counters, container cleanup, and bootstrap deduplication are not coordinated across processes
- Within a single project, at most one `bootstrap` task may run at a time
- The Dispatcher best-effort keeps only one open `bootstrap` intent for a project in the initial state
- Within a single project, at most one `reason` task may run at a time
- Multiple `reason` tasks may run in parallel across projects
- `reason` also counts toward the project's `runtime.max_project_workers`
- `runtime.max_workers`: the cap on the total number of concurrently running tasks in the Dispatcher
- `runtime.max_running_projects`: the admission cap on projects that the current dispatcher runtime has taken over and that are still `active`; even if a project temporarily has no dispatchable task, as long as it is still `active` it keeps occupying a slot until it leaves active
- `runtime.max_project_workers`: the cap on concurrently running tasks within a single project, counting `bootstrap`, `reason`, and `explore` together
- `workers[].max_running`: the concurrency cap of an individual Worker; once reached, this Worker temporarily no longer participates in dispatching

---

## Worker Configuration

### Field Definitions

The Dispatcher always takes the full `stdout` as the model's main output.

The Worker-related runtime fields in `dispatch.yaml`:

| Field | Meaning | Notes |
| --- | --- | --- |
| `name` | Worker static identifier | Used as `creator` or `worker` in protocol writeback |
| `type` | Worker driver name | Supports `claudecode`, `codex`, `mock` |
| `task_types` | Supported task types | `bootstrap`, `reason`, `explore` |
| `max_running` | Worker concurrency cap | No dispatching once reached |
| `priority` | Selection priority | Lower number wins |
| `env` | Runtime environment variables | Used by the corresponding driver; the `mock` driver's per-phase durations and outcome probabilities are also configured here |

The system provides three Worker drivers:

- `claudecode`
- `codex`
- `mock`

In other words:

- `dispatch.yaml` declares "which driver to use, what tasks it can run, how much concurrency, and what environment variables"; for `mock`, the simulated distribution of each phase also lives in `env`
- How health checks are done, how commands are started, how sessions are extracted, and how `conclude` is resumed are all the driver code's responsibility

### Driver Interface

Each Agent / CLI tool corresponds to a separate driver file in the code and implements a unified interface.

The unified interface should at least cover these capabilities:

- `build_healthcheck(worker)`: build the health check command
- `prepare_session()`: pre-generate a session id when needed
- `build_execute(worker, prompt, session)`: build the first-phase execution command
- `extract_session(session, stderr)`: extract the session id from `stderr` when needed, or keep using the pre-generated session
- `build_conclude(worker, prompt, session)`: resume the same session for wrap-up in two-phase `explore`
- `supports_conclude()`: declare whether the driver supports two-phase `explore`

The capability conventions of these drivers:

- `claudecode` supports two-phase `explore`
- `codex` supports two-phase `explore`
- `mock` supports two-phase `explore`

Concurrency modeling convention:

- A Worker should represent one independent LLM concurrency quota unit
- The case of "multiple Workers sharing the same key" is not considered
- Concurrency control uses `workers[].max_running`

### Health Checks

Health checks are implemented by the driver.

The goal is not to verify "the container is alive", but that a specific Worker's LLM configuration is actually usable:

- base URL reachable
- API key valid
- model callable

Execution rules:

- Before each Worker process start, run one health check first
- The `explore_conclude` fallback after `explore` timeout / parse-fail does not repeat the health check; it directly attempts conclude and keeps relying on JSON / schema validation to decide writeback
- Exit code `0` is considered healthy
- Non-`0` / timeout (the Dispatcher's outer watchdog is controlled by `runtime.healthcheck_timeout`) / command cannot start is considered unhealthy
- The driver's internal HTTP probe no longer sets its own timeout; it is uniformly bounded by the Dispatcher's outer watchdog
- If it fails this time, the current task is discarded outright and the Worker is placed into a short unselectable window
- After the window ends, when later rounds select this Worker again, it is checked again
- The Dispatcher records a local `retry_after` for recently failed Workers and does not dispatch to them before that time

The Dispatcher only looks at the exit code and does not need to understand the response body.

### CLI Integration Conventions

#### `claudecode` driver

Depends on environment variables:

- `ANTHROPIC_MODEL`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_AUTH_TOKEN`

The health check can be implemented equivalently inside the driver:

```bash
curl -sS --fail -o /dev/null \
  "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "content-type: application/json" \
  -d "{\"model\":\"$ANTHROPIC_MODEL\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

Known behavior:

- The driver pre-generates a session id
- The first round can specify the session id in advance
- If that id already exists, the command errors out and the old session is not reused
- The Dispatcher always takes the full `stdout` as the result body

First-phase execution:

```bash
claude --session-id "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

Second-phase wrap-up:

```bash
claude -r "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

#### `codex` driver

Depends on environment variables:

- `CODEX_MODEL`
- `CODEX_BASE_URL`
- `OPENAI_API_KEY`

The health check can be implemented equivalently inside the driver:

```bash
curl -sS --fail -o /dev/null \
  "$CODEX_BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "content-type: application/json" \
  -d "{\"model\":\"$CODEX_MODEL\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

Known behavior:

- The first-round session id is printed on `stderr`
- It can be extracted with the regex `session id:\s*([0-9a-fA-F-]+)`
- The Dispatcher always takes the full `stdout` as the result body

First-phase execution:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox --model "{env.CODEX_MODEL}" \
  -c 'model_provider="cairn"' \
  -c 'model_providers.cairn.name="cairn"' \
  -c 'model_providers.cairn.wire_api="responses"' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_providers.cairn.base_url="{env.CODEX_BASE_URL}"' \
  -c 'model_providers.cairn.env_key="OPENAI_API_KEY"' \
  -- "{prompt}"
```

Second-phase wrap-up:

```bash
codex exec resume "{session}" --dangerously-bypass-approvals-and-sandbox --model "{env.CODEX_MODEL}" \
  -c 'model_provider="cairn"' \
  -c 'model_providers.cairn.name="cairn"' \
  -c 'model_providers.cairn.wire_api="responses"' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_providers.cairn.base_url="{env.CODEX_BASE_URL}"' \
  -c 'model_providers.cairn.env_key="OPENAI_API_KEY"' \
  -- "{prompt}"
```

#### `mock` driver

The `mock` driver is used to observe the dispatcher's success, failure, and timeout paths locally.

Behavior conventions:

- With `runtime.prompt_group: "mock"`, the prompt itself is structured JSON and no longer relies on natural-language instructions
- A `reason` prompt contains at least `phase`, `fact_ids`, `open_intents`
- An `explore` / `conclude` prompt contains at least `phase`, `intent_id`
- A `bootstrap` / `bootstrap_conclude` prompt contains at least `phase`, `origin`, `goal`, `hints`
- The driver first parses the `phase` field in the prompt, then reads the corresponding `MOCK_<PHASE>` JSON environment variable to pick the simulated outcome for the current phase
- Each phase configures `delay: [min, max]` in its own JSON; the unit is seconds, decimals supported; when the random duration exceeds the Dispatcher's outer timeout, it naturally manifests as a timeout
- `reason.noop` participates in sampling only when `open_intents` is non-empty; mock automatically avoids outcomes that are invalid in the current context

`mock` supports six phases:

- `healthcheck`
- `bootstrap`
- `bootstrap_conclude`
- `reason`
- `explore_execute`
- `explore_conclude`

Supported outcomes:

- `healthcheck.outcomes`: `ok`, `fail`
- `bootstrap.outcomes`: `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail`
- `bootstrap_conclude.outcomes`: `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail`
- `reason.outcomes`: `complete`, `intent`, `noop`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail`
- `explore_execute.outcomes`: `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail`
- `explore_conclude.outcomes`: `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail`

Naming conventions:

- One variable per phase: `MOCK_<PHASE>`, e.g. `MOCK_REASON`
- The variable value must be a JSON object of the form: `{"delay":[min,max],"outcomes":{...}}`
- `delay` must be two non-negative numbers, in seconds
- All outcome probabilities of a phase are decimals between `0` and `1`, and must sum to exactly `1.0`

### Configuration Validation Rules

#### Load-Time Validation

When starting the Dispatcher, static validation should be completed:

- `dispatch.yaml` must exist and be readable
- `runtime.max_workers` must exist
- `runtime.max_running_projects` must exist
- `runtime.max_project_workers` must exist
- `runtime.interval` must exist
- `runtime.healthcheck_timeout` must exist
- `runtime.worker_healthcheck`, if present, only allows `startup_and_task`, `startup_only`, `disabled`
- `runtime.prompt_group` must exist
- `tasks.bootstrap.timeout` must exist
- `tasks.bootstrap.conclude_timeout` must exist
- `tasks.reason.timeout` must exist
- `tasks.explore.timeout` must exist
- `tasks.explore.conclude_timeout` must exist
- Every Worker must have `type`
- Every Worker must have `max_running`
- `task_types` only allows `bootstrap`, `reason`, `explore`
- `type` only allows `claudecode`, `codex`, `mock`
- `max_running` must be a positive integer
- `claudecode`, `codex`, and `mock` all support two-phase `explore`
- The prompt directory for `runtime.prompt_group` must exist
- The prompt resources in the code project must exist
- Under the default prompt group, `reason.md` must at least cover `{graph_yaml}`, `{fact_ids}`, `{open_intents}`
- Under the default prompt group, `explore.md` must at least cover `{graph_yaml}`, `{intent_id}`, `{intent_description}`
- Under the default prompt group, `bootstrap.md` and `bootstrap_conclude.md` must at least cover `{origin}`, `{goal}`, `{hints}`
- Under the mock prompt group, `reason.md` must at least cover `{fact_ids}`, `{open_intents}`
- Under the mock prompt group, `explore.md` and `explore_conclude.md` must at least cover `{intent_id}`
- Under the mock prompt group, `bootstrap.md` and `bootstrap_conclude.md` must at least cover `{origin}`, `{goal}`, `{hints}`
- The `MOCK_*` variable names of a `mock` worker may only use phases supported by the system
- Each phase probability of a `mock` worker must be between `0` and `1`, and they must sum to exactly `1.0`

#### Runtime Validation

When a task is actually dispatched, runtime validation is also required:

- The driver must exist and support the task type
- When `runtime.worker_healthcheck=startup_and_task`, the driver's health check must succeed before actually starting the task; exit code `0` counts as healthy
- The `explore_conclude` fallback after `explore` timeout / parse-fail no longer repeats the health check; it directly attempts conclude and continues with result validation
- Only Workers that support the current task type can be selected
- Only Workers whose currently running task count is below `max_running` can be selected
- Unhealthy Workers inside their local `retry_after` window do not participate in dispatching
- Both `bootstrap` and `explore` must complete the claim first, and the task thread is started only after success
- For two-phase `explore`, the first phase must successfully obtain a session id before entering `conclude`
- For two-phase `bootstrap`, the first phase must successfully obtain a session id before entering `bootstrap_conclude`
- The Dispatcher must JSON-parse the full `stdout` and perform task-level structure validation
- `accepted: false`, invalid JSON, missing fields, API writeback failure, etc. must all be logged with no immediate retry

---

## Configuration Field Reference

### `dispatch.yaml`

| Field | Required | Meaning |
| --- | --- | --- |
| `server` | Yes | Base URL of the Cairn Server |

### `runtime.*`

| Field | Required | Meaning |
| --- | --- | --- |
| `runtime.max_workers` | Yes | Cap on total concurrently running tasks in the Dispatcher |
| `runtime.max_running_projects` | Yes | Cap on projects taken over by the current dispatcher runtime and still `active`; unaffected by leftover containers from the past |
| `runtime.max_project_workers` | Yes | Cap on concurrently running tasks within a single project, counting `bootstrap`, `reason`, and `explore` together |
| `runtime.interval` | Yes | Unified cadence config; both the Dispatcher main loop interval and the heartbeat interval of claim-carrying tasks |
| `runtime.healthcheck_timeout` | Yes | Unified outer watchdog timeout for Worker health checks |
| `runtime.worker_healthcheck` | No | Worker health check mode: `startup_and_task`, `startup_only`, or `disabled`; default `startup_only` |
| `runtime.execution` | No | Execution backend: `container` (default) or `local`; with `local`, workers run as child processes on the dispatcher host, reusing the host CLIs; startup validates that each CLI is installed and runnable |
| `runtime.prompt_group` | Yes | Directory name of the prompt group currently in use |

### `container.*`

Required only when `runtime.execution: container` (the default).

| Field | Required | Meaning |
| --- | --- | --- |
| `container.image` | Yes | Project container image |
| `container.network_mode` | Yes | Project container network mode |
| `container.completed_action` | Yes | How to handle the container after the project completes |

Allowed values of `container.completed_action`:

- `remove`: remove the container after the project completes
- `stop`: only stop the container after the project completes, preserving the scene

Implementation conventions:

- Container cleanup for completed projects can run asynchronously in parallel and is not required to block the main scheduling loop
- If a project has been deleted from the Server, the Dispatcher treats `cairn-dispatch-*` containers that no longer map to a project as orphans and stop-cleans them

### `local.*`

Only takes effect when `runtime.execution: local`; ignored in `container` mode. In this mode `container.*` is not needed, and workers do not need any LLM environment variables; at startup the Dispatcher probes each configured worker's CLI with `--help`, and exits with an error if all of them are missing.

| Field | Required | Meaning |
| --- | --- | --- |
| `local.workspace_root` | No | Root of the per-project working directories; if unset, the dispatcher's current directory at startup is used, and each project gets an isolated subdirectory `<root>/<project_id>/` as the worker process's working directory |
| `local.completed_action` | No | How to handle the working directory after the project completes: `keep` (default, preserve the scene) or `remove` |

### `tasks.*`

| Field | Required | Meaning |
| --- | --- | --- |
| `tasks.bootstrap.timeout` | Yes | `bootstrap` first-phase timeout |
| `tasks.bootstrap.conclude_timeout` | Yes | `bootstrap` two-phase wrap-up timeout |
| `tasks.reason.timeout` | Yes | `reason` timeout |
| `tasks.explore.timeout` | Yes | `explore` first-phase timeout |
| `tasks.explore.conclude_timeout` | Yes | `explore` two-phase wrap-up timeout |

### `workers.*`

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | Worker static identifier; used as `creator` or `worker` in protocol writeback |
| `type` | Yes | Worker driver name; supports `claudecode`, `codex`, `mock` |
| `task_types` | Yes | List of task types this Worker supports |
| `max_running` | Yes | Concurrency cap of this Worker |
| `priority` | Yes | Among candidate Workers for the current task type, lower number wins |
| `env` | Yes | This Worker's variable table; the required keys are decided by the corresponding driver and validated at startup |

Additionally:

- Worker selection order: first filter by task type, `max_running`, and Workers inside their local `retry_after` window, then by `priority`, ties prefer fewer running tasks, and finally random; both `bootstrap` and `explore` claim first, then start the task; when `runtime.worker_healthcheck=startup_and_task`, one health check runs before actually starting, and failed Workers enter a short unselectable window; entering the `bootstrap_conclude` / `explore_conclude` fallback does not repeat the health check
- Health checks, execution commands, session extraction, and second-phase `conclude` are all handled by the corresponding driver code
- Prompt content is loaded from the markdown resources in the code project

---

## Appendix: Example Configuration and Prompt Content

### `dispatch.example.yaml`

```yaml
server: "http://127.0.0.1:8000"

runtime:
  max_workers: 5  # total running tasks
  max_running_projects: 3  # total active projects admitted by this dispatcher runtime
  max_project_workers: 2  # per-project running tasks, including bootstrap + reason + explore
  interval: 3  # intentional shared cadence: scheduler loop interval + claim-task heartbeat interval, in seconds
  healthcheck_timeout: 15  # shared watchdog for all worker healthchecks, in seconds
  worker_healthcheck: "startup_only"  # startup_and_task | startup_only | disabled
  prompt_group: "default"  # selects prompts/<group>/

tasks:
  bootstrap:
    timeout: 120
    conclude_timeout: 30
  reason:
    timeout: 45
  explore:
    timeout: 600
    conclude_timeout: 120

container:
  image: "tmp:latest"
  network_mode: "host"
  completed_action: "stop"  # options: "remove" | "stop"

common_env:
  TSEC_BASE_URL: "http://<SERVER_HOST>/api"
  TSEC_AGENT_TOKEN: "..."

workers:
  # The reason to split the same model into multiple Workers should be that
  # they use different API keys and therefore have independent concurrency quotas.
  - name: "claude-sonnet-thinker"
    type: "claudecode"
    task_types: [bootstrap, reason]
    max_running: 1
    priority: 0  # lower number wins; ties prefer fewer running tasks, then choose randomly
    env:
      ANTHROPIC_MODEL: "claude-sonnet-4-6"
      ANTHROPIC_BASE_URL: "https://api.example.com"
      ANTHROPIC_AUTH_TOKEN: "sk-ant-worker-a"

  - name: "claude-sonnet-doer"
    type: "claudecode"
    task_types: [bootstrap, explore]
    max_running: 1
    priority: 1
    env:
      ANTHROPIC_MODEL: "claude-sonnet-4-6"
      ANTHROPIC_BASE_URL: "https://api.example.com"
      ANTHROPIC_AUTH_TOKEN: "sk-ant-worker-b"

  - name: "codex-gpt54"
    type: "codex"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 3
    env:
      CODEX_MODEL: "gpt-5.4"
      CODEX_BASE_URL: "https://api.example.com/v1"
      OPENAI_API_KEY: "sk-worker-c"

  - name: "codex-gpt54-alt"
    type: "codex"
    task_types: [bootstrap, explore]
    max_running: 1
    priority: 4
    env:
      CODEX_MODEL: "gpt-5.4"
      CODEX_BASE_URL: "https://api.example.com/v1"
      OPENAI_API_KEY: "sk-worker-d"

  - name: "mock-observer"
    type: "mock"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 9
    env:
      MOCK_HEALTHCHECK: '{"delay":[0.05,0.15],"outcomes":{"ok":0.9,"fail":0.1}}'
      MOCK_BOOTSTRAP: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_BOOTSTRAP_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_REASON: '{"delay":[0.1,2.2],"outcomes":{"complete":0.1,"intent":0.3,"noop":0.1,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.2}}'
      MOCK_EXPLORE_EXECUTE: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_EXPLORE_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
```

Additionally:

- The values here are examples only, showing the configuration structure
- `common_env` is merged into each worker's environment first, then overridden by `worker.env`; i.e., `common_env < worker.env`
- Workers are modeled as independent key / model concurrency quota units; multiple Workers should not share the same key

The following 5 markdown documents correspond to the prompt files in the code project.

### `bootstrap.md`

````md
# Background
The current scenario is an authorized AI penetration testing competition / lab environment.
## Task
You need to solve this problem directly; the goal is to complete the Goal.
## Output Requirements
Return only one raw JSON object, with no other content.
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```
## Context
### Origin
{origin}
### Goal
{goal}
### Hints JSON Array
{hints}
````

### `reason.md`

````md
# Background
The current scenario is an authorized AI penetration testing competition / lab environment.
## Task
You are currently doing `reason` only.
You must judge two things at the same time:
1. Whether the existing facts already satisfy the goal.
2. If not, whether a new intent should be proposed now.
## Output Requirements
Return only one raw JSON object, with no other content.
When refusing the task, return:
```json
{"accepted": false, "reason": "..."}
```
When the goal is satisfied, return:
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```
When the goal is not satisfied but a new intent should be proposed, return:
```json
{"accepted": true, "data": {"intent": {"from": ["f001"], "description": "..."}}}
```
When the goal is not satisfied and no new intent is needed now, return:
```json
{"accepted": true, "data": {}}
```
## Rules
- If `open_intents` below is empty, there is no ongoing exploration in the graph; in that case, if you do not return `data.complete`, you must return `intent`.
- `intent.from` may only be chosen from the valid fact ids below.
## Context
### Graph Snapshot
{graph_yaml}
### Currently Valid Fact ID JSON Array
{fact_ids}
### JSON Array of All Currently Unconcluded Intents
{open_intents}
````

### `explore.md`

````md
# Background
The current scenario is an authorized AI penetration testing competition / lab environment.
## Task
You are currently doing `explore` only.
You handle only this one intent: execute the exploration and give the final factual conclusion.
## Output Requirements
Return only one raw JSON object, with no other content.
When refusing the task, return:
```json
{"accepted": false, "reason": "policy_refusal"}
```
Example of a normal return:
```json
{"accepted": true, "data": {"description": "..."}}
```
## Rules
- `description` must be an objective exploration conclusion; do not output explanatory filler.
## Context
### Graph Snapshot
{graph_yaml}
### Current Intent ID
{intent_id}
### Current Intent Description
{intent_description}
````

### `explore_conclude.md`

````md
# Background
The current scenario is an authorized AI penetration testing competition / lab environment.
## Task
You are currently wrapping up the same `explore`.
- Do not keep exploring.
- Only summarize the exploration and conclusions completed so far.
## Output Requirements
Return only one raw JSON object, with no other content.
When refusing the task, return:
```json
{"accepted": false, "reason": "policy_refusal"}
```
Example of a normal return:
```json
{"accepted": true, "data": {"description": "..."}}
```
## Rules
- `description` must be an objective exploration conclusion; do not output explanatory filler.
## Context
### Graph Snapshot
{graph_yaml}
### Current Intent ID
{intent_id}
### Current Intent Description
{intent_description}
````

### `bootstrap_conclude.md`

````md
# Background
The current scenario is an authorized AI penetration testing competition / lab environment.
## Task
- Do not keep pushing.
- Do not wait for unfinished tasks.
- Only summarize the key facts confirmed so far that are most helpful for reaching the goal.
## Output Requirements
Return only one raw JSON object, with no other content.
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```
## Context
### Origin
{origin}
### Goal
{goal}
### Hints JSON Array
{hints}
````
