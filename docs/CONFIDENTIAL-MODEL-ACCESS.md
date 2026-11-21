# Confidential model access

Cairn supports the same managed model path used by the AdverserialAI ARTEX forks.
The Cairn process and its worker containers never receive the external provider API
key. A model proxy holds that key outside the confidential workload and gives Cairn a
short-lived local capability token instead.

## Trust boundary

```text
external provider key
        |
        v
  model-proxy:8089  <--- local capability token ---  Cairn Dispatcher
        ^                                               |
        |                                               v
  provider API                                  project worker container
```

The runtime provisioner writes a bootstrap document and one token file per worker into
`/run/adverserial/current`. Set `CAIRN_LLM_BOOTSTRAP_FILE` to the bootstrap path before
starting the Dispatcher.

```json
{
  "version": 1,
  "workers": [
    {
      "name": "claude-primary",
      "model": "managed-model",
      "transport": "agent-proxy",
      "base_url": "http://model-proxy:8089/anthropic",
      "token_file": "/run/adverserial/current/claude-primary.token"
    },
    {
      "name": "codex-primary",
      "model": "managed-model",
      "transport": "agent-proxy",
      "base_url": "http://model-proxy:8089/v1",
      "token_file": "/run/adverserial/current/codex-primary.token"
    },
    {
      "name": "pi-primary",
      "model": "managed-model",
      "transport": "agent-proxy",
      "base_url": "http://model-proxy:8089/v1",
      "token_file": "/run/adverserial/current/pi-primary.token",
      "provider_api": "openai-responses"
    }
  ]
}
```

Worker names must match `dispatch.yaml`. The YAML may omit provider model, endpoint,
and key fields in managed mode because the bootstrap supplies them before validation.
Other worker settings, such as task types and Pi context size, remain in YAML.

## Runtime file requirements

The bootstrap and token files must:

- be absolute regular files under `/run/adverserial/current`;
- contain no symlink component;
- be owned by the Cairn process user;
- have no group or other permission bits (`0400` or `0600` are suitable);
- remain within the loader's size limits.

The loader rejects unknown JSON fields, duplicate workers, missing workers, mock
workers, unsupported Pi provider APIs, and all transports except `agent-proxy`. The
only allowed endpoint origin is `http://model-proxy:8089`. Failure stops Dispatcher
startup; managed mode never falls back to credentials in `dispatch.yaml`.

The local token is placed in the environment variable expected by the selected driver:
`ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY`, or `PI_API_KEY`. It is a capability for the
local proxy, not an external provider secret. Avoid logging full worker environments
and rotate the token when a confidential VM owner changes model configuration.

Managed provisioning is intentionally unavailable in local execution mode because
local workers run with the host user's permissions and outside the confidential
container boundary.

## Hosted single-owner deployment

A hosting platform can run Cairn as one owner's deployment inside a confidential machine,
behind a front door that signs the owner in. The `cairn-hosted` image (`Dockerfile.hosted`)
is built for that.

**One image, two processes.** `cairn-hosted server` and `cairn-hosted dispatcher`. Both run
as user 65532 with no way to become root, and keep their state under `/cairn-data`.

**No Docker socket.** The dispatcher runs workers as processes in its own container
(`runtime.execution: local`), using the worker toolbox built into the image. Under a
managed model this needs `CAIRN_MANAGED_LOCAL_WORKERS=1`, which the image sets. Each new
project directory starts from `CAIRN_LOCAL_WORKSPACE_TEMPLATE`.

**The model is connected outside Cairn.** `CAIRN_LLM_BOOTSTRAP_FILE` may name the
platform's own document instead of the per-worker one above:

```json
{"version": 1, "providers": [
  {"provider": "openai", "model": "adverserial/cyberseek", "transport": "agent-proxy",
   "base_url": "http://model-proxy:8089/v1", "api_key_file": "/run/adverserial/current/llm/token"}]}
```

| `provider` | Wire format | Worker kind that runs |
| --- | --- | --- |
| `anthropic` | Anthropic Messages | `claudecode` |
| `openai-responses` | OpenAI Responses | `codex` |
| `openai` | OpenAI Chat Completions | `pi` |

Workers of a kind with no connected provider are not started. An empty `providers` list
means the owner has not connected a model yet: the dispatcher waits, and starts when one
appears. When the document or its token changes, the dispatcher exits with status 75 so
that the deployment's restart policy starts it again with the new model. The same file and
permission rules apply as above. `GET /model` reports what is connected, never a token.

**One user.** With `CAIRN_FRONT_DOOR=1` the server has no accounts of its own: nobody can
register or log in, no API description is published, no other site is allowed to read its
answers, and requests need no Cairn token. Set it only where Cairn's port is reachable by
nothing except the front door and Cairn's own dispatcher.

**Evidence.** `GET /attestation?reportData=<hex>` and `GET /info` return the machine's
hardware evidence from its guest agent (`DSTACK_SOCKET`), and the page's Attestation
dialog checks it from the owner's browser against a verifier. The default verifier is
`CAIRN_VERIFIER_URL`, set when the image is built.
