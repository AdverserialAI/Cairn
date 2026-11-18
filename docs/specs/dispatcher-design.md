# Cairn Dispatcher Design

## Responsibility

The Dispatcher is Cairn's client-side executor. It schedules project work, manages
project execution environments, gives agents explicit tasks, interprets structured
results, and writes those results through the Cairn API.

An agent never claims an Intent, sends a heartbeat, or writes directly to the Cairn
server. The Dispatcher is the only protocol writer and control plane. Agents receive a
rendered prompt and return structured output.

The current implementation assumes one Dispatcher per Cairn server. Multiple
Dispatcher instances sharing one server are not supported or tested.

## Architecture

The system has four parts:

1. **Cairn Server** stores Projects, Facts, Intents, Hints, claims, and reason leases.
2. **Dispatcher** selects tasks and workers, maintains leases, manages timeouts and
   sessions, and writes accepted results.
3. **Project execution environment** is either one container per project or the local
   Dispatcher host.
4. **Worker driver** converts a task into a Claude Code, Codex, Pi, or mock command and
   extracts its response.

```text
                           +-------------------+
                           |   Cairn Server    |
                           | graph + protocol  |
                           +---------+---------+
                                     |
                             structured API
                                     |
                           +---------v---------+
                           |    Dispatcher     |
                           | scheduling/control|
                           +---------+---------+
                                     |
                 +-------------------+-------------------+
                 |                                       |
       +---------v---------+                   +---------v---------+
       | Project container |                   | Project container |
       | agent processes   |                   | agent processes   |
       +-------------------+                   +-------------------+
```

The Dispatcher uses structured project responses for scheduling and protocol writes.
It requests `GET /projects/{id}/export?format=yaml` only to construct graph snapshots
for prompts.

## Execution flow

1. Read active project state from the server.
2. Select `bootstrap`, `reason`, or `explore` according to the scheduling rules.
3. Select an eligible worker and render its prompt.
4. Claim the reserved/ordinary Intent or project reason lease.
5. Start the worker in the project container or as a local subprocess.
6. Keep the claim alive while the process runs.
7. Parse and validate the worker's structured response.
8. Call `complete`, create an Intent, conclude an Intent, or release the claim.

When a project becomes `stopped`, the Dispatcher stops scheduling it, cancels local
tasks, skips conclude fallback for canceled work, and queues container cleanup. A
deleted project receives the same cancellation behavior, but its orphaned container is
removed rather than merely stopped. Cleanup runs outside the main scheduling loop and
can proceed concurrently for several projects.

A reopened project is handled as an ordinary active project with new graph state. If
cleanup from the former completed state is still queued, scheduling waits for cleanup
to finish to avoid racing the old container.

## Task model

### `bootstrap`

Bootstrap is an optional direct solve attempt for a new project. It is eligible when:

- the project is active and `bootstrap_enabled=true`;
- configured workers include bootstrap capability, or a reserved bootstrap Intent
  already exists;
- Facts contain only `origin` and `goal`;
- there are no ordinary Intents.

The reserved Intent uses `from=["origin"]` and `description="bootstrap"`. The
Dispatcher creates it if needed and claims it before starting the worker.

The first phase receives `{origin}`, `{goal}`, and `{hints}`. A successful result must
contain both a key Fact and a completion statement:

```json
{
  "accepted": true,
  "data": {
    "fact": {"description": "The required proof was obtained"},
    "complete": {"description": "The proof satisfies the stated goal"}
  }
}
```

The Dispatcher concludes the reserved Intent first, then completes the project using
the new Fact. If completion fails, the Fact remains and a later reason pass can decide
whether to complete.

On timeout or an unusable response, a session-capable driver may run
`bootstrap_conclude` in the same session. That second phase must summarize only the
best confirmed Fact and must not continue execution. If both phases fail, the
Dispatcher releases the reserved Intent and writes no Fact.

### `reason`

Reason reads the full graph and decides one of three outcomes:

- existing Facts satisfy the goal;
- one new exploration Intent should be declared;
- no graph change is warranted.

It is eligible when the project is active, no claimable Intent should be processed,
and no other reason lease exists. The first pass runs when there are no open Intents.
Later passes run only after new state: more Facts, more Hints, or a transition from
open Intents to none. A released or timed-out exploration that leaves the same open
Intent does not count as new state.

The prompt receives `{graph_yaml}`, `{fact_ids}`, `{open_intents}`, and
`{max_intents}`. Accepted forms are:

```json
{"accepted": true, "data": {"complete": {"from": ["f008"], "description": "Goal met"}}}
```

```json
{"accepted": true, "data": {"intent": {"from": ["f003"], "description": "Inspect the service"}}}
```

```json
{"accepted": true, "data": {}}
```

Reason has one execution phase. Timeout, command failure, rejection, invalid JSON, or
invalid graph references produce no graph write. The Dispatcher releases the reason
lease on every result except successful project completion.

### `explore`

Explore executes one claimed open Intent. Its prompt receives `{graph_yaml}`,
`{intent_id}`, and `{intent_description}`. An accepted result contains one concise,
confirmed Fact:

```json
{
  "accepted": true,
  "data": {"fact": {"description": "The service exposes ports 80 and 443"}}
}
```

The Dispatcher claims the Intent by heartbeat before it starts the worker. A valid
response is written through the conclude endpoint. If the write fails, the Dispatcher
releases the Intent and logs the error without an immediate retry.

Session-capable workers can use a second `explore_conclude` phase after timeout or an
unparseable first response. The Dispatcher terminates the first process, preserves the
session, keeps heartbeats running, and asks the worker to summarize only confirmed
work. Rejection and nonzero process exit are direct failures and do not enter conclude.
Single-phase drivers release the Intent immediately after any failed first phase.

## Scheduling

Global limits come from:

- `runtime.max_workers`: total concurrent tasks;
- `runtime.max_running_projects`: concurrently active projects;
- `runtime.max_project_workers`: concurrent tasks within one project;
- `workers[].max_running`: per-provider concurrency.

Workers are filtered by task capability, capacity, and temporary unhealthy status.
The Dispatcher then selects the lowest numeric priority, prefers the worker with fewer
running tasks on ties, and randomly chooses among remaining ties.

Within a project, bootstrap has priority only in the initial state. New state generally
triggers reason before more exploration; otherwise the Dispatcher consumes claimable
Intents. The server's project-level reason lease enforces one reason pass per project.
The reserved bootstrap Intent enforces one bootstrap pass per project. Different
projects can run those tasks concurrently.

`runtime.interval` is both the main scheduler cadence and the heartbeat cadence for
claimed work. Stable polling, successful heartbeat, and repeated skip decisions are
kept quiet. State changes, container creation, dispatch, process start, health failure,
timeout, fallback, release, and cleanup are logged.

## Configuration

The Dispatcher loads one YAML file. Prompts ship as package resources and are selected
by `runtime.prompt_group`. Drivers own command construction, session behavior, health
checks, and conclude support.

```yaml
server: "http://cairn-server:8000"

runtime:
  interval: 3
  max_workers: 8
  max_running_projects: 3
  max_project_workers: 4
  healthcheck_timeout: 20
  worker_healthcheck: "startup_only"
  execution: "container"
  prompt_group: "default"

tasks:
  bootstrap:
    timeout: 300
    conclude_timeout: 90
  reason:
    timeout: 300
    max_intents: 2
  explore:
    timeout: 300
    conclude_timeout: 90

container:
  image: "cairn-worker:VERSION"
  network_mode: "host"
  completed_action: "stop"
  cap_add: []

workers:
  - name: "claude-primary"
    type: "claudecode"
    task_types: [bootstrap, reason, explore]
    max_running: 2
    priority: 0
    env:
      ANTHROPIC_MODEL: "model-name"
      ANTHROPIC_BASE_URL: "https://provider.example"
      ANTHROPIC_AUTH_TOKEN: "replace-me"
```

### Runtime fields

- `worker_healthcheck` is `startup_and_task`, `startup_only`, or `disabled`.
- `execution` is `container` or `local`.
- `prompt_group` must name a packaged prompt directory with every required template
  and placeholder.

Container execution requires `container.image`, `network_mode`, and
`completed_action`. `cap_add` is optional. Local execution accepts
`local.workspace_root` and `local.completed_action`; workers reuse host CLI
configuration and do not require provider variables in YAML.

`common_env` is merged into each worker environment, with worker-specific values
taking precedence.

### Worker fields

- `name` is unique.
- `type` is `claudecode`, `codex`, `pi`, or `mock`.
- `task_types` is a non-empty unique subset of `bootstrap`, `reason`, and `explore`.
- `max_running` is a positive per-worker limit.
- `priority` is a nonnegative integer; lower values are preferred.
- `env` contains driver-specific settings.

Required container-mode variables are:

| Driver | Required variables |
| --- | --- |
| Claude Code | `ANTHROPIC_MODEL`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` |
| Codex | `CODEX_MODEL`, `CODEX_BASE_URL`, `OPENAI_API_KEY` |
| Pi | `PI_MODEL`, `PI_BASE_URL`, `PI_API_KEY`, `PI_PROVIDER_API` |
| Mock | none |

Pi optionally accepts a positive `PI_MODEL_CONTEXT_WINDOW`.

## Confidential model provisioning

Hosted confidential deployments set `CAIRN_LLM_BOOTSTRAP_FILE` to a process-owned runtime
document under `/run/adverserial/current`. When set, Cairn fails closed unless it is in
container mode and every non-mock worker has exactly one managed entry.

```json
{
  "version": 1,
  "workers": [
    {
      "name": "claude-primary",
      "model": "managed-model",
      "transport": "agent-proxy",
      "base_url": "http://model-proxy:8089/anthropic",
      "token_file": "/run/adverserial/current/claude.token"
    }
  ]
}
```

Only the fixed `http://model-proxy:8089` origin is accepted. Optional paths are
allowed, but user information, query parameters, fragments, path traversal, and
alternate ports are rejected. Pi entries also require `provider_api`, one of
`anthropic-messages`, `openai-responses`, or `openai-completions`.

Bootstrap and token files must be absolute regular files under the runtime directory,
owned by the Cairn process, inaccessible to group and other users, free of symlinks,
and within strict size limits. Unknown fields, duplicate or missing workers, external
URLs, and unsafe files stop startup.

The loader removes all provider endpoint, model, and credential values from the YAML
configuration before it applies managed values. Cairn receives only a short-lived
local capability token. The external provider key remains in the model proxy and is
never stored in Cairn configuration, process arguments, database records, or logs.

See [Confidential model access](../CONFIDENTIAL-MODEL-ACCESS.md) for deployment details.

## Driver behavior

### Claude Code

The driver starts Claude Code with a rendered prompt and captures its structured
result. It supports session continuation, so bootstrap and explore can use conclude
fallback after timeout or parse failure.

### Codex

The driver supplies the configured model and endpoint through Codex configuration and
runs the prompt after `--`. It extracts the response text and session identifier from
Codex output. It supports conclude when a reusable session is available.

### Pi

The driver generates an in-memory provider/model definition from the validated
environment and invokes Pi with the required tools and prompt. The configured provider
API controls request compatibility.

### Mock

The mock driver provides deterministic or weighted outcomes for protocol and scheduler
tests. `MOCK_<PHASE>` variables accept JSON delay ranges, outcome probabilities that
sum to `1.0`, and optional rules. Unknown phases and outcomes are rejected.

## Validation

Startup validates the YAML shape, unique worker names, concurrency relationships,
execution-specific fields, worker environment, prompt group resources, and all prompt
placeholders. Confidential mode adds strict secret and proxy validation.

At runtime, every agent result is parsed according to its task contract. Fact and
Intent references must exist in the current graph, descriptions must be non-empty, and
reason output may not combine completion and new Intent creation. Validation failures
write no graph data and release any held claim.
