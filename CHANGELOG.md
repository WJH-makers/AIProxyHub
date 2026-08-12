# Changelog

## 2.0.0-clean — compliance-focused local gateway refactor

This release changes AIProxyHub to a local-first, user-authorized provider
manager suitable for normal open-source maintenance workflows.

### Removed

- Automated third-party account creation, temporary-mail verification, OTP
  retrieval, fake registration data, anti-bot challenge emulation, and consumer
  OAuth token harvesting.
- Account deletion, account farming, credential rotation, and quota-evasion
  workflows.
- Automatic upstream project/model switching on quota errors; upstream quota
  handling now remains fixed and must follow the provider's normal process.
- Cross-client/API-key response-cache sharing.
- The unused upstream management-center HTML bundle.

### Added or strengthened

- BYOK/BYOA boundaries and explicit provider-authorization documentation.
- Per-client-credential cache isolation, bounded management JSON input, safer
  response headers, and read-only health/usage monitoring.
- Gateway availability limits, enabled by default: a per-client-credential
  sliding-window request rate limit (429 with `Retry-After`) and a global
  in-flight request cap (503). These protect the user's own upstream quota from
  a runaway client; they are not a provider-limit bypass. Rate-limit identities
  are truncated SHA-256 digests and the tracked-identity table is bounded.
- `SECURITY.md`, `PRIVACY.md`, `CONTRIBUTING.md`, `MAINTAINERS.md`, and
  `THIRD_PARTY_NOTICES.md`.
- Fixed-version upstream binary provenance and SHA-256 verification hooks.
- CI checks for tests, compilation, focused linting, secret patterns, and
  removed high-risk registration files.
- Codex-assisted maintenance attribution with human maintainer review.
- A concise `aiph` control script that only manages local lifecycle, status,
  logs, and optional login auto-start; it no longer edits client provider
  configuration or performs external failover.

### Compatibility note

AIProxyHub remains an OpenAI-compatible gateway and may retain upstream
provider/model compatibility names where they are required for routing. Those
names do not imply affiliation, endorsement, or authorization by any provider.

This clean baseline replaces the old public `main` history. Pre-2.0 tags,
releases, and feature branches are withdrawn and unsupported.

Note on history: rewriting a branch does not by itself make the old objects
unreachable on the hosting provider. Pre-rewrite commits may still resolve by
explicit SHA until the provider garbage-collects unreachable objects. See
`SECURITY.md` for what this means and how it is being handled. Any pre-2.0
commit, tag, or artifact should be treated as withdrawn.
