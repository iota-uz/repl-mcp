# Session design audit and 3.1.0 acceptance

## Scope and evidence

This follow-up implements the session, environment, generation, file execution,
broker diagnosis and artifact contracts identified from historical agent sessions.
It reuses the original [63-finding matrix](audit-coverage.md), preserving explicit
product choices: full host access, no sandbox, no Windows support.

The repeat review independently reread 86 indexed episodes in 47 historical
sessions from local Claude Code/Codex and external MacBook/Mac Studio archives.
The indexed episodes predate publication of 3.0.1. Thirteen nearby Python shell
fallback records are temporal correlations, not proof that MCP caused each choice.
T3 database activity/message counts are corpus inventory rather than error rates;
REPL mention matches include irrelevant words and are not execution counts.
No retrospective sample can establish a post-release agent success rate.

## Implemented contracts

| Observed mechanism | Design change | Regression evidence |
|---|---|---|
| Agents overwrite variables/cwd in shared sessions | Independent owned Python sessions; same-project broker pooling | native_sessions; behavioral_evals independent project/concurrency tests |
| Project packages absent in REPL | Explicit interpreter or project .venv, validated Python 3.10+, no silent broken-env fallback | native_sessions environment and broken symlink tests; project dependency behavioral eval |
| Restarted state mistaken for previous namespace | Stable server/session/run IDs, generation guard before effects, safe inventory | crash/reset generation guards; hostile metaclass inventory; retained old generation |
| Python shell chosen for saved scripts | Persistent/fresh saved-file execution, encoding/argv/path/exit semantics | native_artifacts saved-file exit matrix; fresh cancellation and persistent state |
| Missing host bridge mistaken for dead transport | Cached mcp.explain distinguishes grants, registry, credentials, tool and connection state | native_diagnostics; no network probe or credential values |
| Large results copied into conversation or temporary spool | Session-owned artifact references, ranges, atomic save and explicit forwarding | native_artifacts; unicode hash/range behavioral eval; Rust run-bound upload tests |
| Cancelled nested call still writes later | Per-RPC worker/native cancellation, linked run ownership, unknown outcome journal | cancelling_one_call; session/direct artifact forwarding cancellation markers |
| Shared healthy peer discarded on protocol error | Invalidate only a closed transport; retain unrelated calls | shared HTTP protocol error and concurrent cancellation tests |
| Project broker loses durable metadata | Shared bounded journal writer with project/session/run provenance | project registry and cancelled forwarding journal behavioral evals |
| FIFO and slow registry IO block event loop | Reject nonregular inputs; bounded owned blocking jobs for refresh/open | registry and artifact FIFO fixtures; retained physical job permits |

## Local acceptance (macOS arm64, 2026-10-06)

- Complete suite: 342/342 passed with Python 3.10.17 / MCP SDK 2.2.0
  (112.15 seconds), and 342/342 with Python 3.14.7 / SDK 2.3.0
  (112.40 seconds). This includes 127 native worker/protocol/session/broker/
  artifact/behavioral/launcher cases and retained legacy regression evidence.
- Rust: 28 passed, one intentionally ignored guardian subprocess entry exercised
  by ownership tests. Rust fmt, strict Clippy and required Ruff passed.
- Focused rebuilt acceptance: broker/diagnostics 21/21; sessions 14/14; artifacts
  10/10; worker 41/41. The initial complete runs reproduced individual RPC
  cancellation failure (338 passed, one failed), followed by the corrected runs.
- Controlled resource run: 30 session open/execute/close cycles, periodic resets,
  numeric native FDs 9 -> 9, no owned worker process survived shutdown. Initialized
  server RSS was 34,064 KiB after cycle 1, 34,192 KiB at cycles 20 and 30.
  Forty prewarmed arithmetic cells had median 0.448 ms, p95 0.544 ms and max
  0.658 ms; median full session cycle was 72.87 ms. This is a short debug-build
  measurement, not a long-term leak guarantee or a production latency promise.

Distribution and client acceptance are recorded separately in the release/issue
so the local build is not mistaken for an installed published binary.

## Additional defects found while validating the implementation

- Interpreter probe lost stdin ownership when Child.wait() closed it, allowing a
  forged valid probe followed by a hang to terminate early. Retain probe ownership
  until kill/reap; test a valid-looking nonterminating interpreter.
- Cancelled session-open could publish an unused session. Request cancellation
  now drops the staged operation before commit.
- Broken .venv symlinks looked absent and silently selected server Python.
  Detect existence with symlink metadata and fail explicitly.
- Python temporary files for artifact values survived hard worker kills.
  Stream chunks into unlinked native files bound to the originating session/run;
  incomplete upload cleanup runs on completion, cancellation, crash and close.
- Cancelling an asyncio mcp.acall task cancelled only its local Future.
  Propagate individual RPC cancellation without killing another call's transport.
- The Python artifact_forward helper was routed to the storage dispatcher instead
  of the broker, although the public tool worked. Route both through the owned
  session broker task and exercise both interfaces in the same integration test.
- Range reads rehashed the entire immutable artifact for every page.
  Verify once at publication and preserve full integrity verification for save/
  forward; read immutable offset ranges directly.

## Remaining boundaries

A broker cannot discover or inherit host-only cloud connectors or another client's
interactive approvals without an explicit client export/adapter. Direct host tools
remain the supported route. Local HTTP/OAuth fixtures validate the protocol but do
not prove every production provider or model/UI routing behavior. An already sent
write can have an unknown outcome after cancellation; reconciliation is required
before retry. Hard cancellation cannot preserve arbitrary live Python objects or
forcibly interrupt every OS filesystem call. These are explicit contracts rather
than claims of automatic rollback, exactly-once execution, or universal leak freedom.

Artifact refs expire on close, server exit or bounded oldest-first eviction.
A hard server kill during explicit artifact_save can leave an adjacent temporary
file. Retained and unfinished store files themselves are unlinked. Forwarding is
also subject to serialized downstream argument/schema limits.
