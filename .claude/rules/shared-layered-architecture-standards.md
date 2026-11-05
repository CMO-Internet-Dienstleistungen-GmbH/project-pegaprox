# Layered Architecture Standards

Default layering for backend services (Spring Boot / Kotlin, but the shape is language-agnostic). Applies to new services and refactorings — not to upstream PegaProx code, which patches must not refactor.

## Layers

```
Controller  (inbound: REST/SSE) — knows only the *Facade interface, returns only DTOs
  → ServiceFacade — use-case orchestration + Entity/Model → DTO mapping; hides the Services
     → Service — business logic, framework-free
        → Repository — persistence
        → Client — everything external (HTTP APIs, LLM, LDAP, vector store, SSH)
```

## Rules

- **Interfaces without exception.** Every `Service`, `ServiceFacade`, `Repository` and `Client` is an interface plus implementation. Depend on the interface only.
- **Controllers stay at the edge.** A Controller never calls another Controller, talks only to its `ServiceFacade`, handles transport concerns and returns DTOs.
- **The Facade is the DTO boundary.** Entities never reach a Controller or leave over the wire.
- **Services collaborate through interfaces.**
- **`Client` = anything external**, named `*Client`. Framework types stay in the Client / Repository / Controller layer, never in a Service.
- **Every class with behaviour has unit tests.** Pure data holders are exempt.

## Notes

A pragmatic Ports-and-Adapters variant: a framework-independent core with an explicit inward dependency direction, not textbook hexagonal ceremony. Package-by-feature is the default.
