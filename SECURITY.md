# Security policy

## Supported versions

Only the current `main` branch and the latest published release are expected to receive security fixes. Older releases may still contain removed features and should not be used for new deployments.

## Scope and security boundaries

AIProxyHub is a local management layer. It handles provider configuration, a local management API, a client API key, an optional in-memory response cache, logs, and a bundled third-party gateway binary.

- The management UI binds to loopback by default.
- The gateway and management UI bind to loopback by default. Remote gateway binding is opt-in in the configuration, and requires a separate strong `admin_api_key`, a strong client key, and network isolation.
- Sensitive settings are stored with Windows DPAPI where available; plaintext secrets must never be committed.
- The cache is always credential-scoped. It is not a substitute for tenant authorization.
- The gateway enforces a per-client-credential request rate limit and a global in-flight request cap by default. These are local availability guards that protect the user's own upstream quota from a runaway client; they are not a means of evading any provider limit. Rate-limit identities are stored as truncated SHA-256 digests, and the number of tracked identities is bounded so an unauthenticated caller cannot grow the table without limit.
- Upstream quota handling is deliberately conservative: the launcher does not switch projects, model classes, accounts, or external providers to work around an upstream limit. Local failover, if implemented by a user's own client, remains outside AIProxyHub's control plane.
- The health monitor is read-only and must not create, delete, rotate, upload, or harvest provider credentials.
- AIProxyHub does not implement account creation, temporary-mail OTP retrieval, anti-bot challenge emulation, fake identities, consumer OAuth token harvesting, or quota-evasion account rotation.

## Reporting a vulnerability

Please use a private GitHub Security Advisory or another private maintainer contact listed on the repository profile. Do not open a public issue containing credentials, access tokens, exploit code against a live provider, or private prompt/response data.

Include:

1. affected commit/release and operating system;
2. the smallest safe reproduction using dummy credentials and a local test service;
3. impact, prerequisites, and whether the issue crosses the local/remote trust boundary;
4. any proposed mitigation.

The maintainer will acknowledge a valid report, reproduce it in an isolated environment, and publish a fix or mitigation when practical. Please allow reasonable time for coordinated disclosure.

## Pre-2.0 history

Releases before 2.0 contained an automated account-registration workflow
(third-party account creation, temporary-mail OTP retrieval, anti-abuse
challenge emulation, fake registration data, and consumer OAuth token
extraction). That workflow is removed from the current `main`, and the branch
history was rewritten.

**Pre-2.0 tags and their release assets are still published in this
repository.** They are retained for historical continuity, and they still
contain the workflow described above. They are unsupported: do not download,
build, or run them. Only `main` and 2.0+ releases reflect the current,
policy-compliant scope described in the README.

Rewriting `main` did not remove those objects, and it could not have: Git
does not forget on its own. Even if the referencing tags and releases were
deleted, the underlying objects would remain reachable by explicit commit SHA
on the hosting provider until GitHub Support garbage-collects unreachable
objects on request.

No live provider credential was found in the removed code; it used placeholder
values only. Anyone who ran a pre-2.0 build against real accounts should review
those accounts with the relevant provider directly.

## Secret exposure

If a secret is exposed, revoke or rotate it at the provider first, then report the repository path and commit privately. Removing a file from the working tree does not invalidate a secret or erase it from Git history.
