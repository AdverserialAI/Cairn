# Cairn Collaborative Exploration Protocol

## Purpose

Cairn models goal-directed exploration as a directed acyclic graph. Consumers start
from confirmed facts, declare exploration intents, add new facts, and continue until
one or more facts satisfy the goal. A cairn is a trail marker made from stones: each
Fact is one stone that helps later workers continue from earlier work.

The server maintains graph consistency. It does not reason, choose tasks, or run
agents. Humans and agents can concurrently read the graph, declare work, and write
results. The graph is the source of truth for knowledge and causality.

The protocol also has a small amount of project-level coordination state. This state
does not belong to the graph and must not be used as evidence. `Project.reason` is the
only built-in example; it records that one consumer currently holds the lease for a
project-wide reasoning pass.

The design combines several established patterns:

- **Blackboard architecture:** independent workers contribute to shared state.
- **Stigmergy:** workers coordinate by changing the shared environment instead of
  sending messages directly to one another.
- **OODA loops:** every worker observes the graph, orients to its current state,
  decides on an action, and acts.
- **Mission command:** the project defines the goal while consumers decide how to
  reach it. Hints communicate outside judgment without rewriting facts.

Facts are append-only. A state change is represented by a later Fact rather than by
editing an earlier one. For example, `f003` may record that a shell was obtained and
`f025` may later record that the shell disconnected. Their order preserves both
observations and lets a worker decide what to do next.

## Core concepts

### Project

A Project is one problem with a defined origin and goal. Its status is:

- `active`: accepts exploration writes.
- `stopped`: hard-stopped, rejects exploration writes, and can return to `active`.
- `completed`: rejects exploration writes. An external reviewer can call `reopen`
  when the completion decision was incorrect.

### Fact

A Fact is a confirmed objective observation. Facts contain descriptive text, have no
mutable state, and remain in the graph permanently. Large raw output should be stored
outside the graph and referenced from a concise description.

Every project starts with two special Facts:

- `origin`: the known starting state.
- `goal`: the success condition.

The server assigns ordinary identifiers such as `f001`.

### Intent

An Intent is a graph edge describing exploration from one or more Facts. It can be:

- unclaimed: `to=null` and `worker=null`;
- in progress: `to=null` and `worker` identifies the lease holder;
- concluded: `to` identifies the resulting Fact.

`from` accepts multiple Fact identifiers, making the Intent a hyperedge when several
facts jointly support the work. A concluded Intent is immutable.

The server does not define Intent subtypes. Consumers may establish conventions. The
Dispatcher uses an open Intent with `from=["origin"]` and `description="bootstrap"`
for a direct initial solve attempt. It still follows the normal heartbeat, release,
and conclude lifecycle.

### Hint

A Hint is outside input such as a strategy suggestion, constraint, or situation
assessment. It is not evidence and does not participate in graph causality. Hints may
be added while a project is `active`, `stopped`, or `completed`.

## Data model

```text
Project
  id
  title
  status                 "active" | "stopped" | "completed"
  bootstrap_enabled      whether consumers may bootstrap; default true
  created_at
  reason                 current reason lease or null

Project.reason
  worker
  trigger
  started_at
  last_heartbeat_at

Fact
  id                     "origin" | "goal" | generated id
  description

Intent
  id
  from                   non-empty array of Fact ids
  to                     resulting Fact id or null
  description
  creator                immutable author of the direction
  worker                 current holder or final contributor
  last_heartbeat_at
  created_at
  concluded_at

Hint
  id
  content
  creator
  created_at

Settings
  intent_timeout         seconds before an open claim expires
  reason_timeout         seconds before a reason lease expires
```

The server trims leading and trailing whitespace in text fields and Fact identifiers.
An empty value after trimming returns `422`.

For an open Intent, `worker` is the current claim holder. A timed-out open claim is
cleared to `null`; `last_heartbeat_at` is retained. For a concluded Intent, `worker`
permanently identifies the consumer that produced the conclusion.

## API

### Settings

`GET /settings` returns global timeouts. `PUT /settings` replaces them.

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

### Projects

#### `GET /projects`

Returns project metadata and counts without full graph data. Each item includes the
project fields, current `reason` lease, and Fact, Intent, active-work, unclaimed-work,
and Hint counts.

#### `POST /projects`

Creates a project and its `origin` and `goal` Facts. `hints` is optional and
`bootstrap_enabled` defaults to `true`.

```json
{
  "title": "Web application assessment",
  "origin": "Target: http://192.0.2.10",
  "goal": "Obtain the requested proof",
  "bootstrap_enabled": true,
  "hints": [
    {"content": "Inspect the web service first", "creator": "human"}
  ]
}
```

#### `GET /projects/{project_id}`

Returns the complete Project, Facts, Intents, and Hints. Reads also perform timeout
cleanup, so the response reflects current lease ownership.

#### `DELETE /projects/{project_id}`

Deletes the project and all server-side data. The server does not manage execution
containers. A Dispatcher that later notices the deletion cancels local work and
removes the orphaned container.

#### `PUT /projects/{project_id}/title`

Changes the title in any project state. This does not affect graph data, leases, or
status.

```json
{"title": "Web application assessment - review"}
```

#### `PUT /projects/{project_id}/status`

Switches only between `active` and `stopped`. A completed project must use `reopen`.
Stopping a project immediately clears every open Intent claim and the reason lease.
Consumers must treat this as a hard-stop signal, cancel local tasks, and stop the
project container.

```json
{"status": "stopped"}
```

The protocol currently stores only the active Intent claim holder. Clearing claims
therefore removes the identity of the last worker on unfinished work. A future
`worker_history` field could preserve that observability without changing claim
semantics.

### Project-wide reason lease

#### `POST /projects/{project_id}/reason/claim`

Claims the single reason lease for an active project. The same worker may repeat the
request idempotently. Another live holder produces `409`; stopped or completed
projects produce `403`.

```json
{"worker": "dispatcher-worker-a", "trigger": "new_facts"}
```

#### `POST /projects/{project_id}/reason/heartbeat`

Renews the lease for its current holder.

```json
{"worker": "dispatcher-worker-a"}
```

#### `POST /projects/{project_id}/reason/release`

Releases the lease. Releasing an already empty lease is idempotent. A different live
holder produces `409`.

### Hints

#### `POST /projects/{project_id}/hints`

Adds outside guidance in any project state.

```json
{"content": "Review the upload path", "creator": "human"}
```

### Intents

#### `POST /projects/{project_id}/intents`

Declares exploration from one or more Facts. `worker` may be `null` or equal to
`creator`; the latter declares and claims atomically. `from` cannot contain `goal`.

```json
{
  "from": ["f002", "f004"],
  "description": "Use the discovered credentials on the administrative service",
  "creator": "agent-a",
  "worker": null
}
```

#### `POST /projects/{project_id}/intents/{intent_id}/heartbeat`

Claims an unclaimed Intent or renews the current worker's claim. Another live holder
produces `409`. The endpoint applies only to open Intents in active projects.

```json
{"worker": "agent-b"}
```

#### `POST /projects/{project_id}/intents/{intent_id}/release`

Returns an open Intent to the unclaimed state. Release by the current worker succeeds;
release of an already unclaimed Intent is idempotent. `last_heartbeat_at` remains.

#### `POST /projects/{project_id}/intents/{intent_id}/conclude`

Atomically creates a Fact and points the Intent to it. An unclaimed Intent can be
concluded directly; otherwise the caller must hold the live claim.

```json
{
  "worker": "agent-b",
  "description": "The service exposes versions 1.2 and 1.4 on ports 80 and 443"
}
```

#### `POST /projects/{project_id}/complete`

Declares that existing Facts satisfy the goal. The server creates a concluded edge
from those Facts to `goal`, changes the project to `completed`, and clears the reason
lease. `from` cannot contain `goal`.

```json
{
  "from": ["f007", "f012"],
  "description": "The required proof was obtained and independently verified",
  "worker": "agent-a"
}
```

#### `POST /projects/{project_id}/reopen`

Reverses an incorrect completion decision. This management operation is valid only on
a completed project. The server removes the current edge to `goal`, creates a new Fact
with the external correction, links the former completion sources to that Fact with an
`external_feedback` Intent, returns the project to `active`, and clears the reason
lease.

```json
{
  "description": "The submitted proof was invalid; continue searching",
  "creator": "reviewer"
}
```

## Exports

### `GET /projects/{project_id}/export?format=yaml`

Returns a structured graph snapshot for prompt construction. It includes open Intents
and their current workers, but excludes `last_heartbeat_at` and `Project.reason`.
Timestamps use the server's current timezone in `YYYY-MM-DD HH:mm:ss` form.

```yaml
project:
  title: "Web application assessment"
  origin: "Target: http://192.0.2.10"
  goal: "Obtain the requested proof"
  bootstrap_enabled: true

hints:
  - content: "Inspect the web service first"
    creator: "human"
    created_at: "2026-03-21 18:00:00"

facts:
  - id: origin
    description: "Target: http://192.0.2.10"
  - id: goal
    description: "Obtain the requested proof"
  - id: f001
    description: "Ports 22, 80, and 443 are open"

intents:
  - from: [origin]
    to: f001
    description: "Enumerate exposed services"
    creator: "agent-a"
    worker: "agent-b"
    created_at: "2026-03-21 18:01:00"
    concluded_at: "2026-03-21 18:02:00"
```

### `GET /projects/{project_id}/export?format=timeline`

Returns chronological plain text. Event types include `PROJECT CREATED`, `HINT`,
`INTENT DECLARED`, `INTENT CONCLUDED`, and `PROJECT COMPLETED`. A completion removed by
`reopen` no longer appears because its graph edge no longer exists.

## Typical consumer loop

1. Read the structured project to select and coordinate work.
2. Read the YAML export only when a prompt needs the graph snapshot.
3. In the initial state, either claim or create the reserved `bootstrap` Intent, or
   move directly to a reason pass when bootstrap is disabled or unsupported.
4. Prefer an unclaimed ordinary Intent: claim it with heartbeat, keep the claim alive,
   then conclude it or release it.
5. When no work is claimable, claim the project reason lease. A reason pass may call
   `complete`, declare a new Intent, or make no graph change. Release the lease after
   every non-completing result or failure.
6. If outside validation rejects a completed result, call `reopen` with the correction
   and resume the loop.
