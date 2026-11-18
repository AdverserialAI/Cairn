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
