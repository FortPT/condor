# Container packaging

This fork packages the original Condor frontend and Python backend. It is not an
official upstream release or a paper-trading implementation. Application source,
native authentication and trading capabilities are unchanged. Upstream provenance:
<https://github.com/hummingbot/condor>, MIT, initial base
`0ea6e3530909c4362ad4224de5938773aa00e2db`.

## Build and verification

`docker build --build-arg VCS_REF=<commit-sha> -t condor-check .`

The Container workflow builds on pull requests and runs a network-disabled smoke
check of the native SPA pages, authentication rejection and parent/MCP working
directory agreement. Existing upstream CI remains independent. A main-branch push
publishes `ghcr.io/fortpt/condor:sha-<commit-sha>` and `:main` using only the job's
ephemeral GitHub token. Use the resulting immutable digest for deployment; the
moving `main` tag is not a reproducible deployment reference. Verify package
visibility and pull access separately: a public repository does not guarantee a
public container package. These images are built by this fork, not upstream.

Dependencies come from the upstream npm/uv lockfiles, official Node/Python base
images, Debian and PyPI. uv is pinned to 0.11.23. The locked `http-ece` pure-Python
sdist is allowed to build; all other Python dependencies require wheels. Base tags
and Debian repositories may change between builds; retain the published digest.
The final image runs as UID/GID 10001 without a Docker socket or privileged mode.

## Safe initial configuration

`compose.yaml` targets Docker Standalone, not Swarm. It deliberately has no ports
or network and cannot reach any broker, model provider or Hummingbot API. This is
an installation baseline, not a usable networked trading deployment. Review a
specific private ingress and authentication design before enabling access.

Set these deployment variables outside the repository:

| Variable | Meaning |
| --- | --- |
| `CONDOR_IMAGE` | Verified `ghcr.io/fortpt/condor@sha256:<digest>` |
| `ADMIN_USER_ID` | Positive integer for the native local administrator identity |

Native local mode is selected explicitly. The service binds loopback port 8088;
`WEB_URL=http://localhost:8088` also determines the native listening port. Changing
it to HTTPS without an explicit port changes that port to 443. Telemetry and
sharing are disabled. No financial credentials, API server configuration, agent
sessions, scheduled routines or Claude login are supplied. Native local login
still exists: do not expose it without the reviewed access protection. First boot
can generate native application signing/state material inside the dedicated volume;
approve that authentication initialization before a real deployment.

This package adds no Crypto.com Main App adapter, simulated ledger, Claude bridge,
Hummingbot API service, exchange connection or automatic account login. Optional
Claude/other ACP CLIs and browser-dependent exports need separate installation
and authorization; the image does not silently download or authenticate them.
Native trading tools remain in the original product: keeping accounts unconfigured
and network disabled is essential for this baseline. It is not a simulation safety
boundary suitable for later connected accounts.

## Storage, restart, upgrades and rollback

The dedicated `condor-state` volume contains the native config, audit/state files,
data, reports and writable local agents. Code stays in `/opt/condor`; the entrypoint
links it into the persistent working directory so parent and MCP children use the
same native configuration. Conflicting existing paths cause startup to fail rather
than overwrite data. Do not reuse a simulator ledger or another Condor volume.

Restart uses the same state and can resume whatever the original application
persists. This packaging adds no pause-on-restart promise: leave agents/routines
disabled. Upgrade by image digest; do not use the in-app source/git updater inside
the immutable image. Before an upgrade, stop only this service and make a cold
backup of its complete volume with permissions intact. Backups may contain secrets:
store them privately, never in Git, build contexts or CI artifacts. Restore into a
new dedicated volume and verify before selecting it. Roll back to the prior digest
and its matching pre-upgrade backup; native schema downgrade is not guaranteed.

Reset only by selecting a new empty volume after retaining a backup. Never remove
volumes with a general cleanup command. `docker compose down` preserves the named
volume; `down -v` destroys it and is intentionally not part of routine instructions.
