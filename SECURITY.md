# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 2.0.x (development) | ✅ |
| 1.x | ⚠️ security fixes only |

## Threat model

nai is designed to run locally on the owner's machine. Its core privacy
guarantees are:

1. **No outbound runtime traffic** except to the configured local model server
   (`LLAMA_SWAP_URL`) and, for dependency installation only, a package index.
2. **No project content in telemetry** — there is no telemetry.
3. **Allowlisted tools** — models can only call tools named in the job's context pack.
4. **Sandboxed tool writes** — tools write only inside permitted directories
   (by default `outputs/`) and refuse path escapes with `ERROR:`.
5. **Local-only web server** — the dashboard binds to `127.0.0.1` by default,
   uses CSRF tokens, same-origin checks and an optional bearer token.

Multi-user hosting, cloud deployment and Windows-native support are explicit
non-goals. Do not expose the dashboard or MCP server to untrusted networks.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Instead, report it privately:

- Use GitHub's *Security → Report a vulnerability* feature on the repository, or
- Contact the repository owner directly through GitHub.

Include:

- Affected version/commit and component (e.g. `nai.web`, `nai.tools.sandbox`).
- Steps to reproduce and a proof of concept if possible.
- Impact assessment (what data or capability is affected).
- Suggested fix, if you have one.

You should receive an acknowledgement within 7 days. We will keep you informed
of progress and credit you in the fix notes unless you prefer to remain anonymous.

## Scope

In scope:

- Path traversal or writes outside the permitted workspace in file tools and the web browser.
- CSRF/bypass of web state-changing endpoints (`/projects/.../control/*`, approval actions).
- Network calls to destinations other than the configured local model server.
- Injection into prompt templates or stored artifacts that leads to code execution.
- Credential leakage in logs or the store.

Out of scope:

- Vulnerabilities in locally served models or llama-swap itself (report upstream).
- Issues requiring the owner to intentionally run untrusted code or tools.
- Denial of service from exhausting local GPU/CPU resources by the owner's own jobs.

## Safe practices for users

- Keep `LLAMA_SWAP_URL` pointed at loopback; never at a public endpoint.
- Set `NAI_WEB_TOKEN` if anyone else can reach the machine.
- Review tool allowlists in job context packs before enabling new tool families.
- Do not commit `.nai/` databases or call logs containing full prompts to public repositories.
