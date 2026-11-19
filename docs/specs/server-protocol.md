# Cairn Collaborative Exploration Protocol

---

## Essence

Cairn models goal-directed exploration as a directed acyclic graph: starting from known facts, exploration keeps producing new facts until some combination of facts satisfies the goal. The name comes from the stone cairns mountaineers stack as trail markers — every Fact is a stone; agents keep exploring along the cairns left by those before them, while stacking new cairns for those who follow.

This API manages the growth of that graph. Multiple consumers (humans or agents) concurrently read the full graph, each declares exploration intents, each produces new facts, and eventually some consumer judges that the goal has been reached. For very simple problems, a consumer may also make one quick push directly around the origin and goal at the very beginning of the project, without first splitting the work into multiple rounds of explicit planning; this "act right at the start" process still lands on the graph through normal Intents/Facts.

The graph remains the sole expression of knowledge and causal relationships; however, the protocol also allows a small amount of **project-level coordination state**, used for mutual exclusion and visibility among consumers. This kind of state is not a fact, does not belong to the graph, and does not participate in causal reasoning. The only built-in project-level coordination state today is `Project.reason`, which marks that "some consumer is currently making a `reason` judgment over the whole project".

The system does no reasoning and makes no decisions; it is only responsible for maintaining graph consistency.

### Design Origins

This protocol is a modern reconstruction of the **Blackboard Architecture**. In the 1970s, CMU's Hearsay-II system proposed the same paradigm: multiple experts work around a shared blackboard, each reading the current state and each contributing new knowledge, with no central scheduler. A Fact corresponds to content on the blackboard, an Intent corresponds to an expert's action record, and a Hint corresponds to a marginal note. The difference: the classic blackboard holds only the "current state", while this protocol preserves the full causal chain through Intents, making the graph both a knowledge base and an audit log of the reasoning path.

In terms of coordination, this design is equivalent to the ant colony's **pheromone mechanism** (stigmergy) — individuals do not communicate directly, but coordinate indirectly by modifying the shared environment. An agent reading the graph and writing Facts is leaving pheromones in the environment, and other agents decide based on them.

Each consumer's work loop (read graph → judge → declare intent → execute → produce facts) constitutes an independent **OODA loop** (Observe-Orient-Decide-Act), and multiple loops synchronize indirectly through the shared graph. This is isomorphic to **Mission Command** in military command — superiors only issue the objective (goal), each unit decides autonomously based on the situation, and Hints play the role of "commander's intent". The core advantage: decision speed is not limited by a central scheduling bottleneck.

### Expressing State Changes

Facts are append-only; state changes are expressed by appending new Facts. For example, in multi-layer network penetration:

- `f003`: "Obtained a root shell on host-A"
- `f007`: "Discovered the intranet 10.0.2.0/24 via host-A as a pivot"
- `f025`: "The host-A shell has disconnected"

"The shell dropped" is itself a new objective fact. When reading the graph, an agent sees f003 and f025, judges current reachability itself, and declares new exploration intents starting from f025 (rebuild the shell, switch pivots). There is no need to modify historical Facts or mark them invalid — the temporal ordering of facts itself carries the state-change information.

---

## Core Concepts

### Project

A problem instance with a clear origin and goal, containing the complete graph data.

Three states:

- `active`: in progress; accepts exploration writes
- `stopped`: hard-stopped; does not accept exploration writes; can be resumed to `active`
- `completed`: currently complete; does not accept exploration writes; if external confirmation shows the earlier completion judgment was wrong, `reopen` can be explicitly called to revoke the completed state and continue

### Fact

A node in the graph, representing a confirmed objective fact. It has only description text and no state flags. Append-only, permanently retained.

The description text should be a distillation of an exploration conclusion, not raw data. When the raw output is large (e.g., scan results), the description should contain the key insights and a file reference to the raw data, for example: "nmap full-port scan found 22/80/443/8080 open; 80 and 8080 are both HTTP services; detailed results in /tmp/scans/nmap_192.168.1.10.xml". This keeps the graph lightweight while the raw data remains traceable.

Every Project has two special Facts, written when the Project is created:

- `origin`: the starting point
- `goal`: the endpoint

Ordinary Facts get system-generated ids, such as `f001`.

### Intent

An edge in the graph, representing an exploration process starting from one or more Facts. It has three states:

- **In progress**: the Intent has been claimed by some consumer; `worker` is not null
- **Unclaimed**: the Intent has no conclusion yet and its current `worker` is null, waiting for a consumer to pick it up
- **Concluded**: `to` is not null; the exploration is complete and produced a conclusion Fact

An unconcluded Intent (`to=null`) means this exploration direction has been declared but has no conclusion yet. When declaring, one may write only the direction without claiming it, or declare and claim at the same time. A concluded Intent cannot be modified.

The protocol itself does not distinguish "types" of Intent; all Intents are treated identically by the server. However, consumers can agree on certain **reserved semantics**. For example, they may agree that an open Intent with:

- `from = ["origin"]`
- `description = "bootstrap"`

means "one direct push attempt at the very beginning of the project". Such reserved intents still follow the same heartbeat / release / conclude lifecycle and need no extra API; the only difference is that consumers interpret it as "first analyze and execute directly, landing the key proof-of-progress facts on the graph; if those facts are already enough to satisfy the goal, then go on to declare complete", rather than as an ordinary subdivided exploration direction.

This convention suits problems that may be solved quickly, or at least reach a first key breakthrough quickly. The project then remains visible and understandable on the frontend and in audit records: readers can clearly see that the system first made a `bootstrap` attempt and what key facts it produced; results valuable to downstream systems — like flags, shells, and proof of access — do not bypass the fact graph just because the project was completed directly.

**Hyperedge semantics:** `from` allows multiple Fact ids. When `from` contains multiple ids, this Intent is equivalent to a hyperedge in the graph — multiple source nodes jointly point at the same target node, sharing the same description, creator, and worker:

```
f002 ──┐
       ├──(intent)──→ f006
f004 ──┘
```

This fully preserves the causal relationship of "multiple known facts jointly supporting one exploration", instead of being forced to pick one primary Fact and losing the rest of the context.

### Hint

An off-graph input: strategic advice or supplementary notes. It does not belong to the fact graph, does not affect causal relationships, and is provided for consumers' reference when reading the graph.

A Hint represents high-level input supplemented from outside; it is not part of the exploration execution itself. Therefore, when a project is `stopped` or `completed`, adding Hints is still allowed. After a `stopped` project resumes to `active`, subsequent consumers will see these new hints on their next graph read; new Hints on a `completed` project are retained, and if the project needs further processing later, it should still be brought back to `active` via `reopen`.

Besides human-supplied strategic hints, Hints are also suitable for passing **situation assessments** between consumers. As the graph grows, many ruled-out directions dilute the useful information. After finishing a round of exploration, a consumer can write its judgment of the current situation as a Hint (e.g., "SSH and SQL injection directions have both been ruled out; the attack surface is concentrated on the file upload feature"), helping later consumers quickly locate priorities and reducing the cognitive cost of each graph read.

---

## Data Structures

### Project

```
id
title           # project name
status          # "active" | "stopped" | "completed"
bootstrap_enabled  # whether consumers may run bootstrap in the initial state, default true
created_at
reason          # current project-level reason lease; null means nobody is executing reason now
```

### Project.reason

```
worker             # the consumer currently executing reason
trigger            # the trigger of this reason, defined by the consumer
started_at         # when this reason started
last_heartbeat_at  # last reason heartbeat time
```

### Fact

```
id              # "origin" | "goal" | system-generated (e.g. f001)
description     # objective fact description
```

### Intent

```
id
from            # array of source Fact ids, at least one
to              # id of the conclusion Fact; null means no conclusion yet
description     # intent description
creator         # the consumer who declared the intent; written at creation, immutable
worker          # executor identifier; semantics vary with state (see below)
last_heartbeat_at  # last heartbeat time; only meaningful while unconcluded; permanently retained, never cleared
created_at
concluded_at    # conclusion time; null means no conclusion yet
```

`creator` is written at creation and identifies who read the situation and proposed this exploration direction; it is immutable. `worker` may be empty; if the intent is claimed at creation, `worker` must equal `creator`, and the first `last_heartbeat_at` is written.

For text fields in request bodies (such as `title`, `content`, `description`, `creator`, `worker`, `trigger`) and the Fact ids in `from`, the server trims leading and trailing whitespace first; if the result is empty, it returns `422`.

If consumers adopt the `bootstrap` convention, a common practice is to use a fixed `creator`, such as `dispatcher.bootstrap`, to clearly distinguish it from ordinary exploration intents on the graph; this is only a consumer convention, not a server constraint.

**worker field semantics:**

| Intent state                  | worker meaning                                         |
| ----------------------------- | ------------------------------------------------------ |
| Unconcluded, worker=null      | Nobody is handling it now; waiting to be claimed       |
| Unconcluded, worker set       | A consumer is handling it                              |
| Unconcluded, worker timed out | worker is cleared to null; waiting to be re-claimed    |
| Concluded                     | The consumer who produced the conclusion; kept forever |

Timeout-based worker clearing applies only to unconcluded Intents. Concluded Intents do not participate in the timeout logic; worker permanently remains the identifier of the consumer who produced the conclusion.

### Hint

```
id
content
creator         # author of the hint
created_at
```

### Settings

```
intent_timeout  # in seconds; worker is cleared to null if there is no heartbeat within this time
reason_timeout  # in seconds; Project.reason is cleared to null if there is no heartbeat within this time
```

---

## API List

### Global Settings

#### GET /settings

Returns the global configuration.

Response:

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

---

#### PUT /settings

Updates the global configuration.

Body:

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

---

### Projects

#### GET /projects

Returns the list of all projects with metadata and statistical summaries, without graph data.

Response:

```json
[
  {
    "id": "proj_001",
    "title": "xx penetration test",
    "status": "active",
    "bootstrap_enabled": true,
    "created_at": "2026-03-21T10:00:00Z",
    "reason": {
      "worker": "dispatcher-worker-A",
      "trigger": "new_facts",
      "started_at": "2026-03-21T10:06:00Z",
      "last_heartbeat_at": "2026-03-21T10:06:03Z"
    },
    "fact_count": 8,
    "intent_count": 5,
    "working_intent_count": 2,
    "unclaimed_intent_count": 1,
    "hint_count": 3
  }
]
```

---

#### POST /projects

Creates a new project. `origin` and `goal` are written into `facts` as special Facts. `hints` is optional. `bootstrap_enabled` is optional and defaults to `true`; when `false`, consumers skip bootstrap. Even when `true`, consumers without bootstrap capability may go straight to reason.

Body:

```json
{
  "title": "xx penetration test",
  "origin": "Target http://192.168.1.10",
  "goal": "Capture the flag",
  "bootstrap_enabled": true,
  "hints": [
    { "content": "Check the web services first", "creator": "human" },
    { "content": "Watch port 80", "creator": "human" }
  ]
}
```

---

#### GET /projects/{project_id}

Returns the complete project data, including facts, intents, and hints.

Response:

```json
{
  "project": {
    "id": "proj_001",
    "title": "xx penetration test",
    "status": "active",
    "bootstrap_enabled": true,
    "created_at": "2026-03-21T10:00:00Z",
    "reason": {
      "worker": "dispatcher-worker-A",
      "trigger": "new_facts",
      "started_at": "2026-03-21T10:06:00Z",
      "last_heartbeat_at": "2026-03-21T10:06:03Z"
    }
  },
  "facts": [
    { "id": "origin", "description": "Target http://192.168.1.10" },
    { "id": "goal",   "description": "Capture the flag" },
    { "id": "f001",   "description": "Found open ports 22/80/443" }
  ],
  "intents": [
    {
      "id": "i001",
      "from": ["origin"],
      "to": "f001",
      "description": "nmap full-port scan",
      "creator": "agent-A",
      "worker": "agent-A",
      "last_heartbeat_at": "2026-03-21T10:02:00Z",
      "created_at": "2026-03-21T10:01:00Z",
      "concluded_at": "2026-03-21T10:02:00Z"
    },
    {
      "id": "i002",
      "from": ["f001"],
      "to": null,
      "description": "HTTP service probing and directory scanning",
      "creator": "agent-A",
      "worker": "agent-B",
      "last_heartbeat_at": "2026-03-21T10:04:00Z",
      "created_at": "2026-03-21T10:03:00Z",
      "concluded_at": null
    }
  ],
  "hints": [
    { "id": "h001", "content": "Check the web services first", "creator": "human", "created_at": "2026-03-21T10:00:00Z" },
    { "id": "h002", "content": "Watch port 80", "creator": "human", "created_at": "2026-03-21T10:00:00Z" }
  ]
}
```

---

#### DELETE /projects/{project_id}

Deletes the project and all its data.

If some Dispatcher still holds the running container for this project, the container is not managed directly by the Server; when the Dispatcher discovers in a later poll that the project no longer exists, it treats the project as `deleted`, cancels locally running tasks, and removes the corresponding orphan container.

---

#### PUT /projects/{project_id}/title

Changes the project title. This operation does not change the project graph and does not affect the `reason` lease, open intent claims, or project status; the title may be changed whether the project is `active`, `stopped`, or `completed`.

Body:

```json
{
  "title": "xx penetration test (retrospective)"
}
```

Response:

```json
{
  "id": "proj_001",
  "title": "xx penetration test (retrospective)",
  "status": "completed",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": null
}
```

---

#### PUT /projects/{project_id}/status

Updates the project status. Only switching between `active` and `stopped` is allowed. A `completed` project cannot change state again through this API; to revoke the completed state, the dedicated `reopen` API must be called. This API is a project-management operation, not an exploration write.

When a project is switched to `stopped`, the Server immediately clears the `worker` of all unconcluded Intents to `null` and clears `project.reason`, invalidating those claims at once. This way, when the project resumes, the intents can be re-claimed immediately without waiting for timeouts. The semantics of `stopped` is a hard stop: the Server is responsible for rejecting subsequent exploration writes and clearing open intent claims / the reason lease; upon receiving this signal, consumers should immediately cancel locally running tasks and stop the corresponding project container.

Known issue: in the current protocol, the Intent level stores only one piece of information — the "current claim holder", i.e. `intent.worker`. Therefore, once a project is switched to `stopped`, the `worker` of these open intents is cleared immediately; afterwards, the project detail can no longer directly show "which worker was last driving this intent before the stop". A future version may add an Intent-level history field such as `worker_history` to retain this visibility, but the current version has not implemented it.

Body:

```json
{
  "status": "stopped"
}
```

Response:

```json
{
  "id": "proj_001",
  "title": "xx penetration test",
  "status": "stopped",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": null
}
```

---

#### POST /projects/{project_id}/reason/claim

Claims the project-level `reason` lease. It does not belong to the graph and produces no Intent/Fact; it only means "someone is currently making a `reason` judgment over the whole project". At most one `reason` lease may exist per project at a time.

If `project.reason` is currently empty, the claim succeeds and `worker`, `trigger`, `started_at`, and `last_heartbeat_at` are written. If it is currently held by another consumer and has not timed out, 409 is returned. If it is currently held by the same `worker`, the current state is returned idempotently. This API is an exploration write and is allowed only for `active` projects; `stopped` or `completed` returns 403.

Body:

```json
{
  "worker": "dispatcher-worker-A",
  "trigger": "new_facts"
}
```

Response:

```json
{
  "id": "proj_001",
  "title": "xx penetration test",
  "status": "active",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": {
    "worker": "dispatcher-worker-A",
    "trigger": "new_facts",
    "started_at": "2026-03-21T10:06:00Z",
    "last_heartbeat_at": "2026-03-21T10:06:00Z"
  }
}
```

---

#### POST /projects/{project_id}/reason/heartbeat

The current `worker` renews the project-level `reason` lease, updating `last_heartbeat_at`. Only the `worker` currently holding the lease may renew it; if nobody currently holds it, 409 is returned; if it is held by another consumer and has not timed out, 409 is returned. This API is an exploration write and is allowed only for `active` projects; `stopped` or `completed` returns 403.

Body:

```json
{
  "worker": "dispatcher-worker-A"
}
```

---

#### POST /projects/{project_id}/reason/release

The current `worker` actively releases the project-level `reason` lease, immediately returning it to the state where nobody is executing `reason`. Only the current holder may release it; if it is already empty, the current state is returned idempotently; if it is held by another consumer and has not timed out, 409 is returned. This API is an exploration write and is allowed only for `active` projects; `stopped` or `completed` returns 403.

Body:

```json
{
  "worker": "dispatcher-worker-A"
}
```

---

### Hints

#### POST /projects/{project_id}/hints

Adds a strategic hint to the project. Hints are off-graph input writes and are allowed for `active`, `stopped`, and `completed` projects.

Body:

```json
{
  "content": "Try a file upload bypass",
  "creator": "agent-A"
}
```

---

### Intents

#### POST /projects/{project_id}/intents

Declares an exploration intent starting from some Fact, producing an unconcluded Intent. One may declare only, or declare and claim at the same time. Intents are exploration writes, allowed only for `active` projects; `stopped` or `completed` returns 403.

Body:

```json
{
  "from": ["f001"],
  "description": "HTTP service probing and directory scanning",
  "creator": "agent-A",
  "worker": null
}
```

`from` is an array containing at least one Fact id. `worker` may be `null` or equal to `creator`; `null` means declare without claiming, and equal to `creator` means declare and claim immediately. When multiple Facts jointly drive the same exploration, list them all in `from` to fully preserve the causal relationship. For example:

`from` must not contain `goal`.

The server does not validate the business semantics of `description`, so a reserved intent like `description = "bootstrap"` is just an ordinary Intent. Whether to interpret it as "a direct push attempt at the very beginning of the project" is up to consumer convention.

```json
{
  "from": ["f002", "f004"],
  "description": "Log in to the admin panel discovered in f002 using the credentials obtained in f004",
  "creator": "agent-A",
  "worker": "agent-A"
}
```

Response: the newly created Intent object.

---

#### POST /projects/{project_id}/intents/{intent_id}/heartbeat

The consumer reports liveness, updating `worker` and `last_heartbeat_at`. Only valid for unconcluded Intents. This API is used to renew a claim, or to take over an Intent after it timed out and became unclaimed. If the current `worker` is `null`, any consumer may claim via heartbeat; if the current `worker` is held by another consumer and has not timed out, 409 is returned. Heartbeat is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

Consumers usually keep heartbeating at a fixed interval or keep polling the project; after the project enters `stopped` or `completed`, further heartbeats return 403, while polling reads will see the project `status` change. Since the Server immediately clears the `worker` of open intents on `stopped`, consumers should treat it as a hard-stop signal and terminate local progress as soon as possible, rather than continuing to rely on the old claim.

Body:

```json
{
  "worker": "agent-B"
}
```

---

#### POST /projects/{project_id}/intents/{intent_id}/release

The current `worker` actively releases an unconcluded Intent, returning it to the unclaimed state so other consumers can pick it up immediately. Only the current `worker` may release it; if it is held by another consumer and has not timed out, 409 is returned. If the Intent is already unclaimed, the current state is returned idempotently. `last_heartbeat_at` is retained, not cleared. Release is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

Body:

```json
{
  "worker": "agent-B"
}
```

Response: the updated Intent object.

---

#### POST /projects/{project_id}/intents/{intent_id}/conclude

The exploration ends, producing a new Fact and concluding the Intent. Atomic operation. Only valid for unconcluded Intents. The claim rules are the same as heartbeat: an unclaimed intent can be concluded directly; the current `worker` concludes it; if it is held by another consumer and has not timed out, 409 is returned. Conclude is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

`worker`, once written, is permanently retained, identifying who produced this conclusion.

Body:

```json
{
  "worker": "agent-B",
  "description": "Port 80 runs nginx 1.18 with a directory traversal vulnerability"
}
```

Response:

```json
{
  "fact": {
    "id": "f002",
    "description": "Port 80 runs nginx 1.18 with a directory traversal vulnerability"
  },
  "intent": {
    "id": "i002",
    "from": ["f001"],
    "to": "f002",
    "description": "HTTP service probing and directory scanning",
    "creator": "agent-A",
    "worker": "agent-B",
    "last_heartbeat_at": "2026-03-21T10:05:00Z",
    "created_at": "2026-03-21T10:03:00Z",
    "concluded_at": "2026-03-21T10:05:00Z"
  }
}
```

---

#### POST /projects/{project_id}/complete

Called by a consumer after judging that certain Facts jointly satisfy the goal, declaring the project complete. Atomic operation. Complete is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

Creates a concluded Intent whose `from` points at the listed Facts and whose `to` points at `goal`. The completion declaration has no exploration process, so the server sets both `creator` and `worker` to the `worker` value in the request. The Project status becomes `completed`, and the current `project.reason` is cleared immediately.

If later external verification finds this completion judgment was wrong, `POST /projects/{project_id}/reopen` can be called to revoke this completion edge, and the correction information can be written as a new Fact before continuing exploration.

`from` must not contain `goal`.

Body:

```json
{
  "from": ["f007", "f012", "f015"],
  "description": "All three shells have been obtained, satisfying the goal requirement",
  "worker": "agent-A"
}
```

---

#### POST /projects/{project_id}/reopen

When external confirmation shows the project is actually not complete, explicitly revoke the current completed state. This API is a project-management operation, not an exploration write; only `completed` projects may call it.

The server will:

- Find the current unique completion edge with `to=goal`
- Read its original `from`
- Delete this completion edge, keeping no in-graph history of "having been completed once"
- Create a new ordinary Fact whose content is the objective correction note provided by the caller
- Create a new concluded Intent whose `description` is fixed as `external_feedback`, whose `from` inherits the original completion edge's `from`, and whose `to` points at this new Fact
- Set both `creator` and `worker` to the `creator` in the request
- Change the project status back to `active`
- Clear the current `project.reason`

This suits off-graph feedback such as "the submitted flag is wrong; keep looking for the correct flag". The feedback itself is recorded as a new Fact in the graph, while `reopen` is only the control action that triggers this rewrite.

Body:

```json
{
  "description": "The flag FLAG{fake} is wrong; keep looking for the correct flag",
  "creator": "judge"
}
```

Response:

```json
{
  "project": {
    "id": "proj_001",
    "title": "xx penetration test",
    "status": "active",
    "created_at": "2026-03-21T10:00:00Z",
    "reason": null
  },
  "fact": {
    "id": "f016",
    "description": "The flag FLAG{fake} is wrong; keep looking for the correct flag"
  },
  "intent": {
    "id": "i021",
    "from": ["f008"],
    "to": "f016",
    "description": "external_feedback",
    "creator": "judge",
    "worker": "judge",
    "last_heartbeat_at": "2026-03-21T10:10:00Z",
    "created_at": "2026-03-21T10:10:00Z",
    "concluded_at": "2026-03-21T10:10:00Z"
  }
}
```

---

### Export

#### GET /projects/{project_id}/export?format=yaml

Returns a YAML structured snapshot of the project graph for consumers to read. `last_heartbeat_at` is not included. Unconcluded Intents (`to=null`) appear in the YAML, and the `worker` field directly reflects whether a consumer is currently handling them. `project.reason` does not appear in the export, because it is project-level coordination state, not part of the fact graph. The server still performs timeout cleanup on read, so the export reflects the current state after cleanup. `created_at` and `concluded_at` are formatted as `YYYY-MM-DD HH:mm:ss` in the server's current timezone.

```yaml
project:
  title: "xx penetration test"
  origin: "Target http://192.168.1.10"
  goal: "Capture the flag"
  bootstrap_enabled: true

hints:
  - content: "Check the web services first"
    creator: "human"
    created_at: "2026-03-21 18:00:00"
  - content: "Watch port 80"
    creator: "human"
    created_at: "2026-03-21 18:00:00"

facts:
  - id: origin
    description: "Target http://192.168.1.10"
  - id: goal
    description: "Capture the flag"
  - id: f001
    description: "Found open ports 22/80/443"
  - id: f002
    description: "Port 80 runs nginx 1.18 with a directory traversal vulnerability"
  - id: f003
    description: "Traversal discovered /backup/db.sql, containing the user table"
  - id: f004
    description: "Obtained credentials admin/passwd123"
  - id: f005
    description: "SSH login failed; credentials do not work across services"
  - id: f006
    description: "Web admin login succeeded; found a file upload feature"
  - id: f007
    description: "Webshell upload succeeded; obtained a www-data shell"
  - id: f008
    description: "Found flag{abc123} in /var/www/html/flag.txt"

intents:
  - from: [origin]
    to: f001
    description: "nmap full-port scan"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:01:00"
    concluded_at: "2026-03-21 18:02:00"

  - from: [f001]
    to: f002
    description: "HTTP service probing and directory scanning"
    creator: "agent-A"
    worker: "agent-B"
    created_at: "2026-03-21 18:02:00"
    concluded_at: "2026-03-21 18:04:00"

  - from: [f002]
    to: f003
    description: "Download /backup/db.sql via directory traversal"
    creator: "agent-B"
    worker: "agent-B"
    created_at: "2026-03-21 18:04:00"
    concluded_at: "2026-03-21 18:05:00"

  - from: [f003]
    to: f004
    description: "Parse db.sql to extract user credentials"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:05:00"
    concluded_at: "2026-03-21 18:05:30"

  - from: [f004]
    to: f005
    description: "Try SSH login"
    creator: "agent-A"
    worker: "agent-C"
    created_at: "2026-03-21 18:05:30"
    concluded_at: "2026-03-21 18:06:00"

  - from: [f002, f004]
    to: f006
    description: "Log in to the admin panel discovered in f002 using the credentials obtained in f004"
    creator: "agent-A"
    worker: "agent-B"
    created_at: "2026-03-21 18:05:30"
    concluded_at: "2026-03-21 18:06:00"

  - from: [f006]
    to: f007
    description: "Upload a PHP webshell bypassing the extension check"
    creator: "agent-B"
    worker: "agent-B"
    created_at: "2026-03-21 18:06:00"
    concluded_at: "2026-03-21 18:07:00"

  - from: [f007]
    to: f008
    description: "Search for the flag file in the shell"
    creator: "agent-B"
    worker: "agent-A"
    created_at: "2026-03-21 18:07:00"
    concluded_at: "2026-03-21 18:08:00"

  - from: [f008]
    to: goal
    description: "flag{abc123} satisfies the goal requirement"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:08:00"
    concluded_at: "2026-03-21 18:08:01"
```

Example of an unconcluded Intent (a non-null `worker` means a consumer is currently handling it):

```yaml
  - from: [f003]
    to: null
    description: "Try to read sensitive files via directory traversal"
    creator: "agent-A"
    worker: "agent-C"
    created_at: "2026-03-21 18:09:00"
    concluded_at: null
```

---

#### GET /projects/{project_id}/export?format=timeline

Returns the project's timeline as plain text, sorted by event time. The YAML snapshot shows the graph's structural topology, while the timeline shows the chronological order and causal chain of events; the two complement each other. Event types include `PROJECT CREATED`, `HINT`, `INTENT DECLARED`, `INTENT CONCLUDED`, `PROJECT COMPLETED`. If a project was once completed and then `reopen`ed, the timeline does not retain the previous `PROJECT COMPLETED` event, because the old `goal` edge has been revoked.

---

## Typical Consumer Flow

```
1. Read structured project data
   Used for scheduling, state judgment, intent selection, and protocol writeback

2. GET /projects/{id}/export?format=yaml
   Read the graph snapshot (only for the Dispatcher to render to the Agent)

3. If the project is still in its earliest stage:
   The typical test is that facts contain only `origin` and `goal`, and there are no ordinary intents;
   some consumers treat both "no intents at all" and "only the reserved bootstrap intent exists" as the initial state
   If `project.bootstrap_enabled=false`, or the consumer has no bootstrap capability, go straight to reason;
   a. If the reserved bootstrap intent does not exist yet:
      POST /projects/{id}/intents
        { from: ["origin"], description: "bootstrap", creator: "dispatcher.bootstrap", worker: null }
   b. POST /projects/{id}/intents/{intent_id}/heartbeat
        { worker: "dispatcher-worker-A" }
      After a successful claim, run one `bootstrap` round directly
   c. Keep heartbeating while making progress
   d. If a key proof-of-progress fact is obtained:
      POST /projects/{id}/intents/{intent_id}/conclude
        { worker: "dispatcher-worker-A", description: "Found the login page has default credentials admin:admin" }
      If this new fact is already enough to satisfy the goal, you may also continue with:
      POST /projects/{id}/complete
        { from: ["newly written fact id"], description: "why this fact is enough to satisfy the goal", worker: "dispatcher-worker-A" }
   e. If this bootstrap round fails and needs to be abandoned:
      POST /projects/{id}/intents/{intent_id}/release
        { worker: "dispatcher-worker-A" }
   f. Back to step 1

4. If there is an unclaimed ordinary Intent (worker=null) in the graph:
   a. Pick a claimable Intent
   b. POST /projects/{id}/intents/{intent_id}/heartbeat
        { worker: "dispatcher-worker-A" }
      After a successful claim, dispatch one `explore`
   c. Keep heartbeating during the exploration
   d. When the exploration succeeds:
      POST /projects/{id}/intents/{intent_id}/conclude
        { worker: "dispatcher-worker-A", description: "Found error-based injection in the /search parameter" }
   e. If the exploration fails and needs to be abandoned:
      POST /projects/{id}/intents/{intent_id}/release
        { worker: "dispatcher-worker-A" }
   f. Back to step 1

5. If there is no unclaimed Intent in the graph:
   a. POST /projects/{id}/reason/claim
        { worker: "dispatcher-worker-A", trigger: "new_facts" }
      After a successful claim, dispatch one `reason`
   b. Keep POSTing /projects/{id}/reason/heartbeat while making progress
        { worker: "dispatcher-worker-A" }
   c. If `reason` returns a completion conclusion:
      POST /projects/{id}/complete
        { from: ["f008"], description: "flag{abc} satisfies the goal requirement", worker: "dispatcher-worker-A" }
      Flow ends
   d. If `reason` returns a new intent:
      POST /projects/{id}/intents
        { from: ["f003"], description: "Try SQL injection", creator: "dispatcher-worker-A", worker: null }
      Then POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
   e. If `reason` returns neither a completion conclusion nor a new intent:
      POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
      Nothing is written to the graph this round
   f. If `reason` fails, times out, or needs to be abandoned:
      POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
   g. Back to step 1

6. If the project is `completed`, but off-graph verification confirms the earlier completion judgment was wrong:
   a. POST /projects/{id}/reopen
        { description: "The flag FLAG{fake} is wrong; keep looking for the correct flag", creator: "judge" }
   b. The project returns to `active`, with a new `external_feedback` conclusion edge and a correction Fact added to the graph
   c. Back to step 1
```
