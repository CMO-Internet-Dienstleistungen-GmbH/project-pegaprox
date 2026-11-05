---
category: Foundations
description: 'Security-by-design standards: input validation, auth, secrets management, and OWASP prevention'
file_patterns:
    - '**/*'
keywords:
    - security
    - auth
    - authentication
    - authorization
    - xss
    - sql-injection
    - owasp
    - secrets
    - credentials
    - validation
    - csrf
last_updated: "2026-08-15"
subcategory: Security
technologies:
    - security
    - auth
    - api
    - web
topic: security-standards
---
# Security Standards

- Security by design — not bolted on afterwards.
- Validate all external input at system boundaries (API, user input, file uploads). Whitelist over blacklist.
- Parameterized queries always — no string-concatenated SQL. Never implement custom crypto.
- Least privilege: request only permissions actually needed.
- Authorization checks on the backend, on every action — not just in the UI.
- **Never use JWT for authentication** — `alg: none`/key confusion attacks, no server-side revocation. Use opaque server-side session tokens.
- No CDN dependencies — self-host all assets and libraries.
- Rotate and isolate credentials per environment. Never store secrets in code or version control.
- Security events (failed logins, access violations) must be logged with audit trail.

## Secrets in Shell & Kubernetes

- Passing secrets via `--from-literal` in `kubectl create secret` exposes them in `ps` output; use a temporary file and `--from-file` instead.
- Shell environment variables set to 1Password references (`op://...`) are non-empty strings; `setEnvIfEmpty` guards will not override them, causing credential lookup to use the literal reference string instead of the actual secret.
- Terraform-State credentials take authority over shell environment variables; when state provides `access_key`/`secret_key`, they must unconditionally overwrite env vars to prevent 1Password references from being used.

## CI/CD & OAuth

- GitHub Actions workflow push requires the OAuth token to have the 'workflow' scope; fall back to SSH push when the token lacks that permission.

## LDAP & Active Directory

- An AD LDAP filter of `(objectClass=user)` matches computer accounts (`sAMAccountName` ending in `$`) and service accounts in addition to human users. For user-only bulk operations, scope with `(&(objectCategory=person)(mail=*))` or a `memberOf` group filter — otherwise bulk-import/sync jobs pick up non-human principals and create spurious records.

## SSH Agent

- Always use `-o IdentitiesOnly=yes` together with `-i <keyfile>` when the SSH agent has many keys loaded. Without it, the agent offers all keys one by one and the server exhausts `MaxAuthTries` before the correct key is tried — failing with "Too many authentication failures" even though the right key is available.
- Applies equally to: Ansible (`--ssh-extra-args='-o IdentitiesOnly=yes -i …'`), KubeOne (`ssh_private_key_file` + empty `ssh_agent_socket`), direct SSH, and bootstrap scripts. Any tool that shells out to `ssh` inherits the agent's "offer everything" behaviour.

## Promoted (2026-08-15)

- [GOTCHA:0.85] API keys accidentally written into .env.local.example (committed file) instead of .env.local (gitignored) will be exposed in Git history; the key must be rotated immediately even if not yet pushed.
- [PATTERN:0.85] For quick malware triage before setting up a local VM, upload the ZIP to any.run — it executes in a hardened Windows VM and returns process tree, network IOCs, registry writes, and dropped files within minutes.
- [CONVENTION:0.80] Never paste app passwords or secrets into chat; prepare the config command and let the user insert the credential themselves, or use a secret store/env var.
- [DECISION:0.88] Bearer tokens are never returned in plaintext from the settings API; the GET endpoint returns only a boolean *BearerSet flag and masked '***' string; the test-endpoint is server-side only so the browser never sees the token.
- [GOTCHA:0.88] Calling a POST endpoint that triggers destructive server-side mutations 'just to check the HTTP status' causes real data loss. For destructive endpoints (cancel, delete, prune), verify by checking rendered HTML for button presence or asking the user, never by hitting the endpoint.
- [GOTCHA:0.85] Bearer token entropy check requiring ≥3 char classes blocks legitimate long hex/base32 tokens (SHA-1, JWT segments). Fix: accept ≥2 char classes when token length is ≥32.
- [PATTERN:0.85] When a production values file (`values_<app>_prod.yaml`) contains S3 credentials, use targeted `sed` replacements with unique match strings to update specific fields (version, object, sha256) rather than reading or rewriting the whole file — keeps secrets out of the agent context and bash history.
- [CONVENTION:0.80] Use FLAG_IMMUTABLE on all PendingIntents and set allowBackup=false in the manifest; omit INTERNET and analytics permissions for privacy-focused local-first apps.
- [GOTCHA:0.85] SSH with sshpass: using sshpass with the password in an argument exposes it in process listings. Use 'sshpass -e' combined with SSHPASS environment variable to keep the password out of argument strings and shell history.
- [PATTERN:0.90] To set GitLab CI masked variables from local secrets (e.g. Terraform state) without exposing values in shell history, argv, or temp files: pipe the secret directly from `terraform output -json | jq -r '.field.value'` into `glab variable set KEY` via stdin. Never assign to a shell variable or write to a temp file.
- [CONVENTION:0.90] Gitignore SSH password files (e.g. `<host>.pass`) and browser-captured curl dumps (timeline_fetch.sh) that may contain live session tokens before the first commit.
- [GOTCHA:0.75] Shell wrapper functions for password injection are not loaded in non-interactive shells; use SSH_ASKPASS with a 1Password reference instead of plaintext passwords.
- [CONVENTION:0.85] Security tripwire: assert that error paths for external APIs (e.g. Shodan) never propagate the API key into error messages or responses.
- [GOTCHA:0.90] npm audit counts per-package while Dependabot counts per-advisory, so the same vulnerabilities produce very different numbers (15 vs 66). Don't compare them directly.
- [PATTERN:0.90] Secrets written to disk use a `umask 077` temp file that is immediately deleted after use, ensuring credentials never appear in argv, shell history, or log output.
- [GOTCHA:0.90] On macOS, /tmp is a symlink; naive prefix comparison for path traversal checks will reject legitimate requests. Resolve symlinks on both sides before comparing to correctly sandbox file access.
- [GOTCHA:0.90] appendingPathComponent with an absolute path (e.g. '/etc/hosts') silently produces '<root>/etc/hosts', not the intended absolute path — always reject absolute path arguments explicitly rather than redirecting them.
- [PREFERENCE:0.95] Any action on a host machine involving suspected malware — including installing tools, unpacking archives, or running scripts — requires explicit user approval before proceeding, not just before executing the payload itself.
- [PATTERN:0.85] SensitiveOutput interface on tools: tools that return secrets implement it, causing the executor trace to log `redacted=true output_len=N` instead of the plaintext output.
- [GOTCHA:0.93] Production dump files (e.g. prod-dump.txt) containing real customer data (company names, emails) must be added to .gitignore before any git add -A; always scan untracked files for sensitive data before committing a large initial commit.
- [GOTCHA:0.98] GitLab CI `protected` variables (including `REGISTRY_PASSWORD`) are only injected into pipelines running on protected refs. A new release tag pattern (e.g. `v*`) must be explicitly added as a protected tag rule, otherwise release jobs receive empty secrets and fail at login.
- [PREFERENCE:0.75] Use SSH host aliases (e.g. `<host-alias>`) in ~/.ssh/config to enable automatic credential resolution for deployment scripts targeting LAN hosts.
- [CONVENTION:0.88] Write SSH IdentityFile and IdentitiesOnly yes into the config file at mode 0600, and the .pub file at mode 0644 when binding a key.
- [PATTERN:0.90] Separate secret tiers: generated-once secrets (DB passwords, admin passwords) stay on the VM; only externally-coupled secrets (LDAP bind, API keys, webhooks, RabbitMQ) live in 1Password.

