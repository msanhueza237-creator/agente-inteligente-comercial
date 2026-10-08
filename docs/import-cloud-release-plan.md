# Historical import: controlled release proposal

Status: development preparation only. No production configuration changed.
The final, superseding approval proposal is in historical-import-operations.md.

## Verified on 2026-10-07

- SSH and Hostinger Connector access to VPS 1762778 / 187.77.53.52 work.
- Public DNS (1.1.1.1) resolves importaciones.latinchile.cl to that VPS.
- HTTPS verification fails today; there is no Dokploy domain for this host.
- 34 GB free, 83 percent disk utilization. No cleanup authorized.
- Hostinger lists four restore points; newest 2026-10-07 10:35 UTC.
  Thirty-day retention and successful recovery remain unverified.
- Existing app 4f525e6030f6 and workers 3ac4fe0cbb0e / c458cf93aa5c remain running.
- 102 tracked app/runtime files in the live app match agent base 3ae811f exactly.
  pyproject.toml is absent from the runtime, as expected from its Dockerfile.
- The live app image is missing from image inspect. Existing rootfs.tar and
  container-config.json checksums still pass. These alone are not a ready rollback.
- A daily existing Docker image prune can remove unused recovery images. Do not
  change that job under this proposal; protect recovery through a private tar.
- Agent and CRM currently auto-deploy main. No push/merge to main is permitted.
- Existing Dokploy agent environment matches live app values (except the
  intentional Compose override HUB_EMBEDDED_WORKER=false); no credentials were
  printed. The checkout .env is absent; Dokploy's verified deployment builder
  recreates it from its saved environment before the Compose command.
- Traefik websecure, letsencrypt, dokploy-network and watched file-provider
  directory /etc/dokploy/traefik/dynamic exist. No explicit responding timeout
  override is present in static configuration; confirm version defaults before
  rollout without changing global entrypoints.
- CRM HTTPS returns 200; unauthenticated Supabase Auth returns 401.
- profiles has id UUID, active boolean and role enum, with self/admin read RLS.
- No established host-port 8000 connections or references in inspected Traefik
  and cron files. An external request timed out, which does not prove universal
  blocking. Hostinger reports its firewall configuration as not synced.

## Release design, requiring one operational approval

1. Revalidate container IDs, image hashes, disk capacity and no pending deployment.
   Back up narrowly the existing Dokploy settings and relevant configuration in
   a private VPS directory. Preserve all existing environment values; never log
   or commit the configuration, anon key, sessions, or credentials.
2. Materialize a local-only recovery image from the verified rootfs export,
   restoring nonsecret runtime metadata (USER app, WORKDIR /app, CMD, PATH) from
   the container configuration. Do not embed runtime secrets in the image.
   Verify code hashes and Python dependencies in a disposable container with
   network disabled and synthetic settings. Do not start its workers or connect
   it to any database. Export the resulting image with docker image save and
   verify the archive and checksum outside Docker's image-prune scope.
   This prepares recovery, not a production restore. Stop if it cannot be verified.
3. Publish these reviewed deployment files only to a non-main release branch of
   the existing agent repository. No new project, agent, database or worker.
   Build from the verified recovery runtime in a separate preparation step; copy
   only the four reviewed Python files from f7bc8eb. Keep all other runtime
   dependencies and files unchanged. Test the candidate before deployment.
4. In the existing Dokploy Compose resource SaHAMnge3oCj4HNIxCrEc, use that branch
   and docker-compose.import-cloud.yml. Disable auto-deploy for this controlled
   release and save the exact command given in the Compose file. It contains
   only app, --no-deps, --no-build, --pull never, no migrations and no orphan
   removal. HISTORICAL_IMPORT_APP_IMAGE must identify the tested image by full
   immutable image ID. Set COMPOSE_REMOVE_ORPHANS=false in the saved environment.
   Never use general
   Rebuild/Stop/Restart actions on the existing stack. Workers remain outside
   this limited deployment file, on their original images and default network.
5. Reuse the public anon key of the CRM Supabase project, verified to be anon,
   not service_role. Add only IMPORT_SUPABASE_URL, IMPORT_SUPABASE_ANON_KEY,
   IMPORT_ALLOWED_ORIGINS, COMPOSE_REMOVE_ORPHANS=false and the verified
   HISTORICAL_IMPORT_APP_IMAGE. HISTORICAL_IMPORT_BASE_IMAGE is only a build
   argument in the separate preparation step. Preserve HUB_EMBEDDED_WORKER=false and all other
   integration values. Do not paste environment example files.
6. Use repository-managed Traefik labels for an exact POST/OPTIONS preview route.
   Do not also create a broad PathPrefix domain rule in Dokploy. Confirm the
   existing websecure entrypoint, letsencrypt resolver, file-provider directory,
   and upload/read timeout before installing the dedicated transport file.
   Body limit 26 MiB; average 6 requests/minute/IP, burst 3; upstream response
   headers limited to 60 seconds. No public /import, /monitor or /integrations.
   Retain port 8000 only on 127.0.0.1, preserving local server consumers while
   removing direct public publication. No global firewall or DNS changes.
7. Validate backend first: valid TLS, allowed CORS, absent/invalid session
   rejected, forbidden routes not routed. Synthetic authenticated preview only,
   no save RPC, no real contact data, no credential extraction from browsers.
   Verify active administrator/seller and viewer rejection as access permits.
8. Update the existing CRM Dokploy application v8OxI5RagW3PhD2_LmzLc to the
   published fix/historical-import-cloud branch at 0f7e6b8 and compile with
   VITE_HISTORICAL_IMPORT_URL=https://importaciones.latinchile.cl. Preserve its
   existing settings. Disable auto-deploy for the controlled release. No push
   to main. Validate exact bundle, import preview UI and absence of localhost.
9. Verify worker IDs, start times, image IDs and database container remain exactly
   unchanged. Recheck both sites, Supabase and Red Tecnicos. Stop on a mismatch.

## Recovery

No database restore or migration. The single final authorization must include
the limited rollback specified in historical-import-operations.md. Both recovery
images and configurations must be prepared and tested before replacing EITHER
service. Never run compose up on the original full-stack file. The old frontend
has a localhost dependency: rollback restores prior behavior, not cloud operation.

## Remaining limits

Hostinger disk backups are not proof of application-consistent PostgreSQL and
Storage recovery; do not claim tested restoration or 30 days retention. The
admin panel remains HTTP:3000; use SSH transport for administrative API work,
never transmit credentials through its public HTTP URL. Fixing panel HTTPS is
outside this limited release. The local Facto/Chrome accounting automation is
still PC-dependent; this release removes only the historical-preview dependency.

## Local validation completed

- Agent f7bc8eb: 288 tests passed, three deprecation warnings. All outside-network
  connections blocked; Windows event-loop loopback sockets permitted. Test
  database URL was synthetic, pointing to unused local port 1.
- CRM 0f7e6b8: all 19 historical transport tests passed; prospecting contract OK.
- CRM typecheck/build passed with the intended HTTPS import origin. Vite warns
  of existing large bundles; no build failure.
- Focused ESLint passed for the two changed import modules and transport tests.
- Existing server Docker Compose validated the proposed manifest read-only,
  without resolving a nonexistent .env: only app, no depends_on, port bound to
  127.0.0.1 and both intended networks. No deployment or container creation.
- Runtime uses FastAPI 0.141.1; local tests use 0.139.0. Final disposable-image
  validation against the actual recovered runtime is still a pre-rollout gate.
- Installed Dokploy v0.29.14 command formatter was read from its own source map
  and evaluated separately without importing the live server. It preserves the
  custom command exactly. Builder source SHA256:
  e9901d20a550a7a349d14fe05bb3e5689cfdaf63b05d8a6148d787ccb57edd7a.
- Compose v5.1.4 dry-run on the existing project returned 0 and planned only app
  recreation. Worker/db orphan warning does NOT request their deletion. All
  container IDs, image IDs, start times and states were identical before/after.
  The simulation used stdin, synthetic environment and an already-existing
  stand-in image; it did not test the future image or its missing .env. The exact
  rendered configuration and actual image require another dry-run after approval.
- The frontend's running image and current tag both resolve to
  sha256:e3711f591560ace9370476ab7fc7740f7d1f702c798ac87c440e32209d55e93b.
  Its private saved-image archive and isolated startup test are NOT yet prepared.
- No main update, production configuration write, backup restoration, migration,
  worker interruption, or real-data preview was performed.

## References

- https://docs.dokploy.com/docs/core/docker-compose
- https://docs.dokploy.com/docs/core/docker-compose/domains
- https://raw.githubusercontent.com/Dokploy/dokploy/v0.29.14/packages/server/src/utils/builders/compose.ts
- https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/buffering/
- https://supabase.com/docs/reference/javascript/auth-getuser
