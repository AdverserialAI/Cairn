---
name: tsec-actions
description: Submit a flag for a TSEC CTF or authorized intelligent penetration-testing challenge.
---

## Usage

* Call the API directly with `curl`. `TSEC_SERVER_HOST` and `TSEC_AGENT_TOKEN` are already available as environment variables.
* Flags use the `flag{...}` format.

Submit a flag:

```bash
curl -X POST "http://${TSEC_SERVER_HOST}/api/submit" \
  -H "Agent-Token: ${TSEC_AGENT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"code": "<challenge_code>", "flag": "<flag>"}'
```

## Rules

- Submit a flag only with high confidence. Do not use the endpoint for brute forcing.
- Treat the API response as the authoritative result; do not assume that a submission succeeded.
