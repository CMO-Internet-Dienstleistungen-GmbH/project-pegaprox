# Technology Assessments

Cross-project technology decisions. Follow them when recommending or using these technologies.

## Banned / dead

- **MinIO** — effectively dead as open source (early 2026). Evaluate SeaweedFS (Apache 2.0) or Garage (AGPLv3) for S3-compatible storage.
- **CentOS Stream** — rolling, not suitable for production. Use Rocky Linux for RHEL-family requirements.
- **JWT for authentication** — use opaque, revocable server-side session tokens instead.
- **@tolgee/react / @tolgee/web** — banned as runtime i18n dependencies in Next.js projects. Use next-intl.

## Language preferences

- **Spring Boot WebFlux (Kotlin) for long-running services** — daemons, microservices and background servers. Go remains acceptable for small CLIs and one-shot tooling; Python for NumPy/ML workloads and quick scripts. (This repository is the exception by nature: it automates a Python upstream.)
- **Bash over Python for scripts** where bash suffices; Python when bash is insufficient (complex data structures, API clients).

## Local tooling vs. delivered tooling

A personal secret store (1Password, a keychain) is a local convenience, never a dependency of what ships. Scripts, playbooks and CI jobs read secrets from **environment variables or stdin**, so the caller decides where they come from. Anyone without that tool — a colleague, a runner — must be able to run the same artifact unchanged.

## Preferred choices

- **Terraform over OpenTofu** for new roots, unless a written-down need requires OpenTofu. State encryption at rest is not that reason: its passphrase lives in the same secret store as the credentials the state describes. Treat a state that holds secrets as key material instead.
- **PlantUML for diagrams in Gradle/JVM projects**; Mermaid where the target renders it natively. One format per repository.
- **smol-toml** — TOML parser for TypeScript/ESM projects.
- **WireMock 3.x** (`org.wiremock:wiremock-standalone`) — Kotlin/Gradle integration tests.
- **flant/redis-sentinel-proxy** (v2.0.5+) — Sentinel proxy for Kubernetes; linux/amd64 only.
- **@sapphi-red/web-noise-suppressor** — browser RNNoise WASM.
- **Liberation Sans** — license-free font for server-side text rendering.
- **archiver** (npm) — streaming ZIP creation.
- **System fonts** over web or CDN-hosted fonts.
- **Qdrant** — self-hosted vector store, used as a rebuildable search projection next to a relational system of record. Accessed over REST via a reactive client. **Milvus is unsuitable**: its default deployment depends on MinIO.
