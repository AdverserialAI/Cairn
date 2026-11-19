---
name: tsec-actions
description: Use when submitting a flag for a challenge in the tsec CTF / intelligent penetration testing competition
---

## Usage

* Call the API directly with `curl`; TSEC_SERVER_HOST and TSEC_AGENT_TOKEN are already present in the environment variables
* Flags always use the `flag{...}` format

Submit a flag:

```bash
curl -X POST "http://${TSEC_SERVER_HOST}/api/submit" \
  -H "Agent-Token: ${TSEC_AGENT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"code": "<challenge_code>", "flag": "<flag>"}'
```

## Rules

- Only submit a flag when you have high confidence; do not treat this as a brute-force endpoint
- Treat the API response as authoritative for flag submission; never assume a submission succeeded on your own
