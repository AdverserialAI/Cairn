# Releasing Cairn

A release is a semantic version tag on `main`:

```bash
git checkout main
git pull --ff-only
git tag v0.2.2
git push origin v0.2.2
```

The release workflow verifies that the tagged commit belongs to `main`, builds the
Cairn application and worker images for Linux AMD64, attaches SBOM and provenance
attestations, pushes both images to Artifact Registry, and signs each digest with a
keyless Sigstore identity tied to this repository, workflow, and tag. The resulting
GitHub release lists both immutable image references and verification commands.

No registry password, cloud service-account key, or signing key is stored in GitHub.
The workflow exchanges GitHub's short-lived OIDC token through the repository-scoped
`cairn` Workload Identity Federation provider.

After the workflow succeeds, verify both signatures using the release notes. Add the
two digest-pinned image references to the approved release data in
`AdverserialAI/agent-deployment`, then regenerate the confidential VM configuration.
The image digests become part of the deployment inputs used to derive the expected
measurement. Existing VMs remain on their current digests until they are replaced.

Tags and mutable aliases are convenient discovery names only. Confidential deployments
must use the full `repository@sha256:...` reference published in the release.
