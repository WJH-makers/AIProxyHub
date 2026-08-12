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
- Cross-client/API-key response-cache sharing.
- The unused upstream management-center HTML bundle.

### Added or strengthened

- BYOK/BYOA boundaries and explicit provider-authorization documentation.
- Per-client-credential cache isolation, bounded management JSON input, safer
  response headers, and read-only health/usage monitoring.
- `SECURITY.md`, `PRIVACY.md`, `CONTRIBUTING.md`, `MAINTAINERS.md`, and
  `THIRD_PARTY_NOTICES.md`.
- Fixed-version upstream binary provenance and SHA-256 verification hooks.
- CI checks for tests, compilation, focused linting, secret patterns, and
  removed high-risk registration files.
- Codex-assisted maintenance attribution with human maintainer review.

### Compatibility note

AIProxyHub remains an OpenAI-compatible gateway and may retain upstream
provider/model compatibility names where they are required for routing. Those
names do not imply affiliation, endorsement, or authorization by any provider.

This clean baseline is intended to replace the old public `main` history.
The pre-refactor history is retained only in the local recovery bundle created
before publication; it is not part of the rewritten `main` branch.
