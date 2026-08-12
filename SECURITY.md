# Security policy

## Supported versions

Only the current `main` branch and the latest published release are expected to receive security fixes. Older releases may still contain removed features and should not be used for new deployments.

## Scope and security boundaries

AIProxyHub is a local management layer. It handles provider configuration, a local management API, a client API key, an optional in-memory response cache, logs, and a bundled third-party gateway binary.

- The management UI binds to loopback by default.
- Remote binding is opt-in and must use a separate strong `admin_api_key` plus network isolation.
- Sensitive settings are stored with Windows DPAPI where available; plaintext secrets must never be committed.
- The cache is always credential-scoped. It is not a substitute for tenant authorization.
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

## Secret exposure

If a secret is exposed, revoke or rotate it at the provider first, then report the repository path and commit privately. Removing a file from the working tree does not invalidate a secret or erase it from Git history.
