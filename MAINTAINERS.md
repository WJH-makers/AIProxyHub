# Maintainers

## Primary maintainer

- `WJH-makers` — repository owner and primary maintainer. Sole maintainer at the time of writing; there is no wider maintainer team.

## Maintained scope

The maintainer is responsible for AIProxyHub itself, not for any upstream project it integrates:

- the Windows local management panel and its authentication boundary;
- gateway lifecycle management, port-safe startup, and automatic port reassignment;
- the OpenAI-compatible forwarding layer and request normalization;
- per-client-credential cache isolation and gateway rate/concurrency limits;
- read-only health and usage monitoring;
- tests, regression coverage, and release smoke checks;
- security, privacy, and third-party notice documentation;
- CI, release workflows, packaging, and upstream binary provenance.

AIProxyHub bundles [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (MIT). Its maintainers are separate people; issues in the upstream binary belong upstream. Nothing in this repository grants access rights to any AI provider.

## Tool-assisted maintenance

OpenAI Codex may assist with maintenance analysis, test design, documentation, and code review. The primary maintainer reviews and verifies every accepted change. Codex is not a copyright holder, maintainer, co-author, sponsor, or endorser of AIProxyHub.

## Maintainer workflow

- Pull requests: run focused tests, compile checks, secret-pattern checks, and a human review before merge.
- Issues: reproduce locally with dummy credentials or a local fake service; never ask for provider secrets or private prompts in a public issue.
- Releases: pin the bundled CLIProxyAPI version, verify its SHA-256 provenance, build the Windows artifact, run the release smoke test, and publish checksums plus third-party notices.
- Security: use private GitHub Security Advisories, keep the management surface loopback-only by default, and review credential/cache boundaries before accepting changes.

AIProxyHub is an independent project and is not affiliated with or endorsed by OpenAI, CLIProxyAPI, or any configured provider.
