# Audit #9 implementation coverage

Patch 3.0.1 addresses Claude Code catalogue compatibility by adding top-level
`ttlMs: 0` and `cacheScope: "private"` to `tools/list` responses for all supported
protocol versions. The evidence below records the 3.0.0 migration;
patch-specific protocol and packaging checks are recorded separately at release.

Coverage for version 3.0.0, validated locally on macOS on 2026-10-05/06.
Distribution checks and runtime acceptance are distinguished from live agent UI
evaluations below. See the [release procedure](../CI.md#release-artifacts) and
[upgrade checklist](../CHANGELOG.md#migration-from-2x).

Evidence: **270/270 Python tests passed** on macOS with Python 3.10.17 / MCP SDK
2.2.0, and **270/270 passed** with Python 3.14.7 / SDK 2.3.0. **15 Rust unit tests
passed**; one ignored internal guardian subprocess entry is exercised by ownership
tests. Rust fmt, strict clippy, required Ruff and skill validation passed.

Evidence keys: P = `tests/test_native_protocol.py` (15 real stdio tests),
B = `tests/test_native_broker.py` (10 local stdio/HTTP/OAuth cases),
W = `tests/test_native_worker.py` (30 worker cases),
R = Rust unit tests in `native/src/`. The full suite also retains the legacy
Python engine tests and two SDK stdio launch harnesses. These do not claim a
live Claude Code, Codex or t3 model/UI session evaluation.

The controlled resource probe observed one idle native server and no Python child.
After 20 resets server numeric FDs stayed **12 -> 12**; saved completed Popen was
reaped, no owned zombies or processes remained after shutdown. Debug-build RSS
was 28,064 KiB idle, 32,640 KiB initialized server and 32,976 KiB after resets;
this short probe does not establish absence of every long-term leak. Child RSS
includes shared pages and must not be treated as unique physical memory.
Temporary probe artifacts are under `~/Workbench/outputs/repl-mcp-rust/`.

Release packaging passed: a **5.9 MiB macOS arm64 binary wheel** was built,
installed in a fresh environment and exercised over stdio; it selects the sibling
Python interpreter, imports the pinned httpx/openpyxl and returns semantic errors.
The source archive was built and installed from its own contents. Release binary
passed all **25 protocol/broker tests**. Release resource probe also kept FDs
**12 -> 12** over 20 resets with no owned zombies/survivors; server RSS was
19,696 KiB idle, 24,176 KiB initialized and 24,400 KiB after resets. This is a short
controlled measurement, not a guarantee about all workloads or long-term leaks.

Rows below distinguish fixes from explicit product decisions and remaining limits.

| Finding | Problem | Status / evidence |
|---|---|---|
| 1 | P1 · R/C · Ошибки Python выглядят как успешный MCP tool result. | P: structured_error_and_schema; MCP isError and structured execution failure |
| 2 | P1 · R/C · mcp.call теряет isError. | P: bridge_envelopes; ToolError retains envelope |
| 3 | P1 · R/C · mcp.call теряет structuredContent и все блоки после первого. | P: bridge_envelopes; complete multiblock/structured response |
| 4 | P1 · R/C · mcp.help отдаёт пустую схему при допустимом MCP SDK 2.2.0. | P: schemas; B: HTTP fixtures; R: schema validation |
| 5 | P1 · T/C · Синхронный mcp.call конфликтует с описанием top-level await. | W/P: sync await executes exactly once; acall provides async concurrency |
| 6 | P2 · C · Наружу утрачен структурный ExecutionResult. | P: structured result/outputSchema |
| 7 | P2 · C · Discovery не обходит next_cursor. | P: two-page catalogue; bounded cursors |
| 8 | P2 · C · Ошибка listing превращается в пустой каталог. | B: oversized catalogue fails explicitly; config errors fail closed |
| 9 | P2 · C · Зарезервированные имена мешают передать законные аргументы чужого инструмента. | P: arguments envelope preserves reserved names |
| 10 | P2 · T/C · Данные требуют ручного JSON-парсинга и дополнительных транспортных обёрток. | P: structured results; mcp.text is explicit extraction |
| 11 | P1 · R/C · Отмена execute не отменяет ячейку. | P/W: cancellation checks absence of late filesystem effects |
| 12 | P1 · C/T · Прерывание ячейки не отменяет соответствующую parent RPC task. | P: linked slow_write cancelled; broker request cancellation guard |
| 13 | P1 · R/C · EOF/OSError в grace-await обходят recovery. | P/R: grace exit and crash recovery; owned teardown |
| 14 | P1 · C/T · Мёртвый ClientSession остаётся connected. | R: session invalidation; no automatic write retry |
| 15 | P2 · C/H · Закрытие transport не гарантируется из-за ownership anyio tasks. | Native owned peer/guardian and explicit kill/reap replace anyio lifecycle |
| 16 | P2 · C/H · Жизненный цикл shell descendants не привязан к ячейке. | W/P: shell groups, managed Popen reaping, detached group cleanup |
| 17 | P2 · T/C · Несогласованная лестница таймаутов. | W: helper deadline regression; bounded broker connect/call/execute budgets |
| 18 | P2 · C · Общий help не является надёжно дешёвым. | Cheap name-only help and independent health do not fan out |
| 19 | P2 · T/C · reset=True не восстанавливает зависший мост и не прерывает очередь. | P: independent cancel; reset restarts worker; overlap fails instead of queueing |
| 20 | P2 · C · Нет журнала эффектов/частичного результата вложенных вызовов. | B: bounded durable metadata journal, unfinished dispatch -> outcome_unknown; no exactly-once promise |
| 21 | P1 · T/C · Persistent globals не означают persistent async runtime. | W/P: persistent loop, locks and idle DNS/default executor survive cells |
| 22 | P1 · C · Лимиты вывода действуют после полного накопления. | W/P: capture bounded during writes; flood and UTF-8 tests |
| 23 | P1 · R/C · Каждая ячейка вызывает repr всех пользовательских globals. | W/P: no implicit custom repr or global enumeration |
| 24 | P2 · C · Нет общего ceiling для ресурсов и входа. | R/P: code/frame/RSS/concurrency limits; sampled worker RSS is not whole-tree quota |
| 25 | P2 · C/H · IPC send способен блокировать server loop. | R: bounded owned writer actor, cancellation cannot split an accepted frame |
| 26 | P2 · C · Время выполнения недооценивает реальную стоимость. | Native wall timing includes capture/postprocessing; resource probe records client wall time |
| 27 | P2 · C/T · Crash/grace-kill уничтожает накопленное состояние. | W: explicit bounded JSON checkpoint/restore. Arbitrary Python object restoration remains unsupported |
| 28 | P2 · C · C/native вызовы не гарантируют сохранение namespace при timeout. | P: GIL-blocking native execution killed after grace; state cleared honestly |
| 29 | P2 · C · После hard-kill/crash теряется ещё не доставленный stdout/stderr. | P: crash retains native fd output; bounded drain before final result |
| 30 | P2 · C · Reset очищает словарь, но не всё состояние среды. | Restart restores launch cwd/environment/async runtime rather than dictionary-only reset |
| 31 | P2 · C · Нет стабильных cell/run IDs и source history. | W/P: run/session/generation IDs, bounded source history and linecache |
| 32 | P2 · C · Невозможен multiprocessing в daemon kernel. | P: multiprocessing spawn succeeds; worker is not daemon multiprocessing child |
| 33 | P2 · C · Fan-out bridge фактически сериализован. | W: multiplexed async calls; broker concurrency bounded at 32 |
| 34 | P1 · C · Нет исполняемой policy boundary вложенных tools. | B: per-call allowedTools; explicit independent broker grant |
| 35 | P1 · C · Реальная изоляция ограничивается subprocess crash boundary. | Product decision: user explicitly requires full host access, no isolation (2026-10-05) |
| 36 | P1 · C · Discovery заимствует настройки Claude даже у других клиентов. | Default project registry; foreign client scopes require explicit opt-in/grant |
| 37 | P1 · C · Deny-only merge не равен клиентской политике. | Independent grant/allowlist contract; foreign interactive approvals are not inherited |
| 38 | P2 · T/C · Нет пути OAuth/credential capabilities текущего host-клиента. | B: independent OAuth PKCE, CSRF/issuer rejection, rotation and client credentials |
| 39 | P2 · C · Конфиги известных/подключённых серверов не обновляются. | B: TTL/manual refresh; invalid registry repaired without disabling Python |
| 40 | P2 · C · Поддержана не вся форма plugin-конфигурации. | Explicit exported plugin registry supported; automatic foreign plugin registry parsing intentionally unsupported |
| 41 | P2 · C · Shadow aliases могут создавать отдельные sessions/процессы. | B: equivalent aliases share peer PID but preserve separate allowlists |
| 42 | P2 · C · Config validation допускает молчаливое неверное поведение. | R/B: strict fields, URL/env/header/cwd/transport validation |
| 43 | P2 · C/H · Redaction не покрывает все ошибки/пути. | B: private OAuth store, symlink/public permissions rejected; journal has metadata only |
| 44 | P2 · T/C · Инструкции разных установок противоречат версии 2.x. | Canonical repo skill and plugin updated to 3.x. Upgrade runtime and deployed skill/plugin copies together using the migration checklist |
| 45 | P2 · T/C · Инъецированный mcp легко спутать с import mcp. | Skill/docs distinguish injected broker object from importable Python MCP SDK |
| 46 | P2 · T/C · Установка пакетов не воспроизводима и может попасть не в тот env. | Wheel uses sibling interpreter; explicit --python and sys.executable install target; locked source env |
| 47 | P2 · C/T · Pinned Git tag не фиксирует dependency/runtime graph. | Cargo/uv lockfiles and full pinned wheel runtime dependency closure; caller-selected CPython is explicit |
| 48 | P3 · R/C · Обещание '~ all work' неверно без expanduser. | Docs require Path.expanduser; no unsupported implicit tilde claim |
| 49 | P2 · C · Нет доступной диагностики runtime/health вне выполнения кода. | P/B: independent health, lazy Python, degraded registry diagnosis |
| 50 | P2 · C/T · Нет поддержанного long-run progress/wait/cancel. | P: python_start returns run ID immediately; bounded live polling, independent cancel |
| 51 | P3 · T/C · Ошибки перегружены helper traceback и двойными префиксами. | W: source-attributed bounded traceback; broker errors redact secret values |
| 52 | P3 · C · Лимиты описаны как KB, фактически это chars. | W/P/B: UTF-8 and serialized frame byte limits |
| 53 | P3 · C · Обещания скорости/ожиданий слишком абсолютны. | Removed blanket latency/SLA claims; observed resource measurements only |
| 54 | P3 · C/H · Globals server kernel/mcp_wrapper осложняют embedding. | Owned per-server Rust state replaces source legacy globals in shipped runtime |
| 55 | P1 · C · CI не запускает ключевые новые kernel/hardening suites. | CI runs complete suite plus strict Rust/Python checks; package source build job |
| 56 | P2 · R/C · Протокольные тесты не проверяют смысл ошибок. | P/B: real stdio/HTTP semantic error/schema/result fixtures |
| 57 | P2 · C · Нет реалистичных client-matrix / behavioural evals. | Local Python 3.10/3.14 and SDK 2.2/2.3; CI adds Linux/3.12. Live Claude/Codex/t3 UI acceptance remains rollout work |
| 58 | P2 · C · Regression тест interruption проверяет канал, но не отсутствие поздних эффектов. | W/P: cancelled threads/callbacks/RPC do not create delayed marker files |
| 59 | P2 · C · Нет сохранённых agent-facing failure traces и контрольных evals для steering. | Saved behavioural fixtures reproduce sync-await, reserved args, shadowed helpers, timeout and result-envelope pitfalls; live model evals not claimed |
| 60 | P3 · C · Заявленная MIT лицензия не оформлена. | MIT LICENSE added; native/Python/package metadata consistent |
| 61 | P1 · R/C · Cleanup lifespan пропускается при исключении/cancellation из обслуживаемого блока. | R/P: owned cleanup on shutdown/drop/client cancellation; forced parent death tests |
| 62 | P1 · R/C · Executing kernel переживает принудительную смерть parent. | P: native guardian handles Rust SIGKILL with GIL-blocked worker/peer and managed detached children |
| 63 | P2 · C/измерение · Kernel и multiprocessing resource_tracker создаются eagerly для каждого экземпляра MCP. | Resource probe: idle server only; lazy worker/guardian and no multiprocessing resource tracker |

## Additional bugs found and fixed during implementation

- Cancellation during an IPC write could split a JSON frame: owned four-frame writer
  actor completes accepted frames; a controlled backpressure regression aborts the
  caller mid-write and checks both that frame and the next command.
- A guardian attached after child creation left a spawn/registration gap and reused
  bare PID risk: guardian now starts first and owns the child/group until reaping.
- Polling progress and completing a run acquired active/output locks in opposite
  order: snapshots release the active lock before reading output.
- Cancellation could briefly leave the execution slot reserved: callers wait only
  for already-requested cancellation cleanup; ordinary overlap still fails promptly.
- Timed-out schema jobs released their quota before actual blocking work stopped:
  the real closure owns a bounded permit; at most four CPU jobs can remain active.
- Journal fsync held the metadata mutex and cancellation could schedule excess work:
  a single coalescing writer owns one physical I/O slot and releases metadata locks
  before filesystem operations. Controlled stalled-I/O regression checks access.
- Tokio runtime destruction waited for blocking stdin after SIGTERM with the client
  still connected: explicit runtime shutdown budget exits, and the open-stdin
  SIGTERM acceptance checks parent/worker termination.
- Cancelled asyncio wrappers hid live executor work, and old callbacks/threads could
  borrow a later cell: actual futures and run context are tracked; late-effect tests
  cover threads, timers, stale context and RPC completion.

## Explicit limits / rollout work

No sandbox is required by the user. Arbitrary manual forks/native process detachment
and custom signal changes are trusted code, not a containment guarantee. RSS is a
sampled worker budget; descendants and native server caches have separate bounded
contracts, not a hard whole-tree memory quota. Already running schema validation
and OS fsync cannot be interrupted by a future timeout; concurrency remains bounded
and native process shutdown has an independent deadline.

Hard native termination clears state. JSON checkpoints deliberately do not restore
functions/imports/resources. Remote writes can remain `outcome_unknown`; cancellation
is not a transaction and the broker never automatically retries a write. Automatic
foreign plugin registry parsing has been replaced by explicit exported grants.
Windows is unsupported. Linux/3.12 are configured in CI, not executed on this Mac.
Live Claude/Codex/t3 model/UI sessions and real-provider OAuth are not claimed by
the local fixtures. Updating a runtime alone does not update separately installed
2.x skill/plugin copies; upgrade those together using the migration checklist.
