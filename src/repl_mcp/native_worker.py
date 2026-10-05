"""Dependency-free, trusted-local Python worker for the Rust supervisor.

This is a crash boundary, not a sandbox. The supervisor owns reset, execution
deadlines and memory budgets. Frames are private newline-delimited JSON v1.
"""

import ast
import asyncio
import concurrent.futures
import contextlib
import contextvars
from collections import OrderedDict
import io
import json
import linecache
import math
import os
import queue
import selectors
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
import weakref

MAX_FRAME = 1024 * 1024
OUTPUT_BYTES = 50_000
PREVIEW_BYTES = 20_000
MAX_PENDING_RPC = 64
CHECKPOINT_BYTES = 256 * 1024
CHECKPOINT_ITEMS = 256
RESERVED_NAMES = {"sh", "mcp", "checkpoint", "restore", "repl_history", "repl_source", "__name__", "__builtins__", "__repl_result__"}
RUN_CONTEXT = contextvars.ContextVar("repl_run", default=None)
SOURCE_RUNS = 64
SOURCE_BYTES = 1024 * 1024


def clean_text(text: str) -> str:
    return text.encode("utf-8", "replace").decode("utf-8")


def prefix(text: str, budget: int) -> str:
    """Bound encoding allocations even if the original string is huge."""
    return text[:budget].encode("utf-8", "replace")[:budget].decode("utf-8", "ignore")


def safe_value(value, depth=0, budget=None):
    """Inspect only exact builtin types; never invoke user repr/iter/property."""
    if budget is None:
        budget = [100]
    budget[0] -= 1
    if budget[0] < 0 or depth > 5:
        raise ValueError("preview depth or item limit")
    kind = type(value)
    if value is None or kind is bool:
        return value
    if kind is int:
        if value.bit_length() > 4096:
            raise ValueError("large integer")
        return value
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value
    if kind is str:
        if len(value) > 2000:
            raise ValueError("large string")
        return clean_text(value)
    if kind in (list, tuple, HistoryResult):
        if len(value) > 100:
            raise ValueError("large collection")
        return [safe_value(item, depth + 1, budget) for item in value]
    if kind is dict or kind is BridgeResult:
        if len(value) > 100 or any(type(key) is not str for key in value):
            raise ValueError("large or non-JSON dictionary")
        return {safe_value(key, depth + 1, budget): safe_value(item, depth + 1, budget)
                for key, item in value.items()}
    raise ValueError("custom object")


def preview(value):
    if type(value) in (str, ShellResult):
        text = str.__str__(value)
        retained = prefix(text, PREVIEW_BYTES)
        rendered = json.dumps(retained, ensure_ascii=False)
        shortened = prefix(rendered, PREVIEW_BYTES)
        truncated = len(retained) != len(text) or shortened != rendered
        return shortened, retained if not truncated else None, truncated
    try:
        converted = safe_value(value, budget=[512] if type(value) is HistoryResult else [100])
        rendered = json.dumps(converted, ensure_ascii=False, allow_nan=False)
        shortened = prefix(rendered, PREVIEW_BYTES)
        return shortened, converted, shortened != rendered
    except ValueError:
        kind = type(value)
        # Bypass custom metaclass attribute access as well as custom repr.
        name = type.__getattribute__(kind, "__name__")
        suffix = ""
        if kind in (str, bytes, list, tuple, dict, set, frozenset):
            suffix = f"; length={len(value)}"
        return f"<{name}{suffix}; safe preview omitted>", None, True


class BoundedCapture(io.TextIOBase):
    def __init__(self, worker, run_id: str, stream: str, limit=OUTPUT_BYTES):
        self.worker, self.run_id, self.stream = worker, run_id, stream
        self.remaining = limit
        self.parts = []
        self.truncated = False
        self.lock = threading.Lock()

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError("write() requires str")
        with self.lock:
            retained = prefix(text, self.remaining)
            used = len(retained.encode("utf-8"))
            self.remaining -= used
            if len(retained) != len(text):
                self.truncated = True
            if retained:
                self.parts.append(retained)
                self.worker.send({"type": "output", "id": self.run_id,
                                  "stream": self.stream, "text": retained})
        return len(text)

    def getvalue(self):
        with self.lock:
            return "".join(self.parts)


class ToolError(RuntimeError):
    """A downstream MCP failure with the original envelope available."""
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


class BridgeResult(dict):
    """Full envelope that tolerates legacy ``await mcp.call(...)`` once."""
    def __await__(self):
        async def completed():
            return self
        return completed().__await__()


class HistoryResult(list):
    """Known safe metadata collection with at most 64 entries."""


class OwnedExecutor(concurrent.futures.ThreadPoolExecutor):
    """Track actual default-executor work, rather than harmless idle threads."""
    def __init__(self):
        super().__init__(max_workers=min(32, (os.cpu_count() or 1) + 4), thread_name_prefix="repl-asyncio")
        self.owned = {}
        self.owned_lock = threading.Lock()

    def submit(self, function, /, *args, **kwargs):
        origin = RUN_CONTEXT.get()
        with self.owned_lock:
            if len(self.owned) >= 64:
                raise RuntimeError("Too many pending default executor jobs (limit 64)")
        future = super().submit(function, *args, **kwargs)
        with self.owned_lock:
            self.owned[future] = origin
        def completed(done):
            with self.owned_lock:
                self.owned.pop(done, None)
        future.add_done_callback(completed)
        return future

    def cancel_run(self, identifier):
        with self.owned_lock:
            jobs = [future for future, origin in self.owned.items() if origin == identifier]
        for future in jobs:
            future.cancel()
        # cancel() cannot stop a running concurrent Future; it stays owned until
        # the real callable completes. Never rely on its cancelled asyncio wrapper.
        return any(not future.done() for future in jobs)


class MCPBridge:
    def __init__(self, worker):
        self.worker = worker

    def _future(self, op, params):
        return self.worker.rpc(op, params)

    @staticmethod
    def _wait(future, timeout):
        try:
            return future.result(timeout=timeout)
        finally:
            if not future.done():
                future.cancel()

    @staticmethod
    def _check(result):
        if isinstance(result, dict) and result.get("isError"):
            messages = [part.get("text", "") for part in result.get("content", [])
                        if isinstance(part, dict) and part.get("type") == "text"]
            raise ToolError("MCP tool failed: " + prefix("\n".join(messages), 2000), result)
        return BridgeResult(result) if isinstance(result, dict) else result

    @staticmethod
    def _arguments(arguments, kwargs):
        if arguments is not None and kwargs:
            raise ValueError("Use either arguments={...} or keyword tool arguments, not both")
        result = arguments if arguments is not None else kwargs
        if not isinstance(result, dict):
            raise TypeError("arguments must be a JSON object")
        return result

    @staticmethod
    def _timeout(timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.01 <= timeout <= 3600:
            raise ValueError("MCP timeout must be finite and between 0.01 and 3600 seconds")
        return timeout

    def call(self, server, tool, *, arguments=None, timeout=120.0, **kwargs):
        """Sync call returns the full MCP envelope; use acall inside await."""
        timeout = self._timeout(timeout)
        return self._check(self._wait(self._future("call", {
            "server": server, "tool": tool,
            "arguments": self._arguments(arguments, kwargs), "timeout": timeout,
        }), timeout + 1))

    async def acall(self, server, tool, *, arguments=None, timeout=120.0, **kwargs):
        """Async call permits concurrent bridge operations on the persistent loop."""
        timeout = self._timeout(timeout)
        future = self._future("call", {"server": server, "tool": tool,
                                      "arguments": self._arguments(arguments, kwargs),
                                      "timeout": timeout})
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(future), timeout + 1)
            return self._check(result)
        finally:
            if not future.done():
                future.cancel()

    def servers(self):
        return self._wait(self._future("servers", {}), 35)

    def list_tools(self, server, *, timeout=30.0):
        """Discover tools using one bounded connect/catalogue budget."""
        timeout = self._timeout(timeout)
        return self._wait(self._future("tools", {"server": server, "timeout": timeout}), timeout + 1)

    def refresh(self):
        return self._wait(self._future("refresh", {}), 35)

    def journal(self):
        """Inspect bounded dispatch/outcome records; never automatically retry writes."""
        return self._wait(self._future("journal", {}), 35)

    async def ajournal(self):
        """Async effect journal inspection within the originating active cell."""
        future = self._future("journal", {})
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), 35)
        finally:
            if not future.done():
                future.cancel()

    @staticmethod
    def text(envelope):
        """Join all text blocks while the original envelope remains available."""
        if not isinstance(envelope, dict):
            raise TypeError("mcp.text expects a full result envelope")
        return "\n".join(part.get("text", "") for part in envelope.get("content", [])
                         if isinstance(part, dict) and part.get("type") == "text")

    def help(self, server=None, tool=None, *, timeout=30.0):
        """Return complete tool schemas without connecting unrelated servers."""
        if server is None:
            return self.servers()
        tools = self.list_tools(server, timeout=timeout)
        if tool is None:
            return tools
        entries = tools.get("tools", []) if isinstance(tools, dict) else tools
        for entry in entries:
            if entry.get("name") == tool:
                return entry
        raise ValueError(f"Unknown tool {tool!r}; use mcp.list_tools(server)")


class ShellResult(str):
    def __new__(cls, stdout, *, returncode, stderr, truncated=False):
        instance = super().__new__(cls, stdout)
        instance.returncode, instance.stderr = returncode, stderr
        instance.truncated = truncated
        return instance

    @property
    def stdout(self):
        return str(self)

    @property
    def ok(self):
        return self.returncode == 0


class ShellError(RuntimeError):
    def __init__(self, result):
        self.result = result
        self.returncode, self.stdout, self.stderr = result.returncode, result.stdout, result.stderr
        super().__init__(f"Shell exited with status {result.returncode}; inspect exception.stderr")


class Worker:
    def __init__(self, output=None, input_stream=None):
        self.output = output or os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
        self.input = input_stream or sys.stdin.buffer
        self.write_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = {}
        self.commands = queue.Queue(maxsize=2)
        self.children = set()
        self.children_lock = threading.Lock()
        self.tracked = weakref.WeakSet()
        self.tracked_lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.executor = OwnedExecutor()
        self.loop.set_default_executor(self.executor)
        self.executing = False
        self.run_id = None
        self.sources = OrderedDict()
        self.source_bytes = 0
        self.namespace = {"__name__": "__repl__", "mcp": MCPBridge(self), "sh": self.sh,
                          "checkpoint": self.checkpoint, "restore": self.restore,
                          "repl_history": self.repl_history, "repl_source": self.repl_source}
        self.install_process_tracking()

    def remember_source(self, identifier, source):
        filename = f"<repl:{identifier}>"
        lines = source.splitlines(keepends=True)
        weight = sys.getsizeof(source) + sys.getsizeof(lines) + sum(map(sys.getsizeof, lines))
        while self.sources and (len(self.sources) >= SOURCE_RUNS or self.source_bytes + weight > SOURCE_BYTES):
            _, old = self.sources.popitem(last=False)
            self.source_bytes -= old["weight"]
            linecache.cache.pop(old["filename"], None)
        if weight <= SOURCE_BYTES:
            self.sources[identifier] = {"source": source, "lines": len(lines), "filename": filename, "weight": weight}
            self.source_bytes += weight
            linecache.cache[filename] = (len(source), None, lines, filename)

    def repl_history(self):
        """List bounded source metadata; source bodies are never included by default."""
        return HistoryResult({"run_id": identifier, "lines": entry["lines"]} for identifier, entry in self.sources.items())

    def repl_source(self, run_id):
        """Explicitly retrieve local source retained within 64 runs / 1 MiB."""
        entry = self.sources.get(run_id)
        if entry is None:
            raise ValueError("Run source unknown or evicted (source history: 64 runs / 1 MiB)")
        return entry["source"]

    def install_process_tracking(self):
        """Poll owned Popen objects without stealing wait() exit status.

        The wrapper keeps standard Popen behavior and uses weak references. It
        cannot track manual os.fork(), native subprocess libraries or detachment
        outside Popen. Those remain trusted-code lifetime responsibilities.
        """
        worker = self
        original = subprocess.Popen

        class TrackedPopen(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._repl_group = bool(kwargs.get("start_new_session") or kwargs.get("process_group") == 0)
                self._repl_registered = False
                if self._repl_group:
                    worker.send({"type": "job", "id": worker.run_id, "pid": self.pid, "action": "started"})
                    self._repl_registered = True
                with worker.tracked_lock:
                    worker.tracked.add(self)

        subprocess.Popen = TrackedPopen

        def reap():
            while True:
                with worker.tracked_lock:
                    tracked = list(worker.tracked)
                for process in tracked:
                    if process.poll() is not None:
                        if process._repl_registered:
                            worker.stop_child(process)
                            worker.send({"type": "job", "id": worker.run_id, "pid": process.pid, "action": "finished"})
                            process._repl_registered = False
                        with worker.tracked_lock:
                            worker.tracked.discard(process)
                # CPython retains dropped live Popen objects here to reap later;
                # its cleanup updates their returncodes instead of waitpid(-1).
                subprocess._cleanup()
                time.sleep(0.05)

        threading.Thread(target=reap, name="worker-child-reaper", daemon=True).start()

    def checkpoint(self, path):
        """Atomically save bounded JSON-safe variables; never pickle or replay code.

        Imports, functions, custom objects and large/deep values are reported as
        skipped. Call explicitly before risky work; snapshots may contain secrets.
        """
        values, skipped = {}, {}
        count = 0
        for name, value in self.namespace.items():
            if name in RESERVED_NAMES:
                continue
            if count >= CHECKPOINT_ITEMS:
                break
            count += 1
            if type(name) is not str or len(name) > 256:
                continue
            try:
                candidate = safe_value(value)
                # Keep the entire snapshot bounded, including JSON escaping.
                trial = {**values, name: candidate}
                if len(json.dumps(trial, ensure_ascii=False).encode("utf-8")) > CHECKPOINT_BYTES - 16384:
                    raise ValueError("snapshot byte limit")
                values[name] = candidate
            except ValueError as exc:
                skipped[name] = str(exc)
        record = {"format": "repl-checkpoint-v1", "values": values, "skipped": skipped,
                  "omitted": max(0, len(self.namespace) - sum(name in self.namespace for name in RESERVED_NAMES) - count)}
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > CHECKPOINT_BYTES:
            raise ValueError("Checkpoint exceeds 256 KiB; reduce variable count")
        target = os.path.abspath(os.path.expanduser(os.fspath(path)))
        temporary = target + "." + uuid.uuid4().hex + ".part"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        return {"saved": len(values), "skipped": skipped, "omitted": record["omitted"]}

    def restore(self, path):
        """Validate a bounded JSON checkpoint before merging safe variables.

        Existing other variables remain. Helpers cannot be replaced. No modules,
        functions, classes, tasks, open handles or external effects are restored.
        """
        with open(os.path.expanduser(os.fspath(path)), "rb") as stream:
            encoded = stream.read(CHECKPOINT_BYTES + 1)
        if len(encoded) > CHECKPOINT_BYTES:
            raise ValueError("Checkpoint exceeds 256 KiB")
        record = json.loads(encoded)
        if type(record) is not dict or record.get("format") != "repl-checkpoint-v1":
            raise ValueError("Unsupported checkpoint format; expected repl-checkpoint-v1")
        values = record.get("values")
        if type(values) is not dict or len(values) > CHECKPOINT_ITEMS:
            raise ValueError("Checkpoint values must contain at most 256 variables")
        validated = {}
        for name, value in values.items():
            if type(name) is not str or len(name) > 256 or name in RESERVED_NAMES:
                raise ValueError("Checkpoint contains invalid or reserved variable names")
            validated[name] = safe_value(value)
        self.namespace.update(validated)
        return {"restored": len(validated), "state": "merged"}

    def send(self, frame):
        encoded = json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        if len(encoded) > MAX_FRAME:
            raise ValueError("Worker frame exceeds 1 MiB; reduce code/arguments/result")
        with self.write_lock:
            self.output.write(encoded)

    def rpc(self, op, params):
        origin = RUN_CONTEXT.get()
        if origin is None or origin != self.run_id:
            raise RuntimeError("MCP calls must belong to their originating active cell; use mcp.acall for concurrency")
        identifier = uuid.uuid4().hex
        future = concurrent.futures.Future()
        with self.pending_lock:
            if len(self.pending) >= MAX_PENDING_RPC:
                raise RuntimeError("Too many pending MCP calls (limit 64)")
            self.pending[identifier] = (origin, future)
        try:
            self.send({"type": "rpc", "id": identifier, "run_id": origin,
                       "op": op, "params": params})
        except BaseException:
            with self.pending_lock:
                self.pending.pop(identifier, None)
            raise
        # Completion/cancellation drops references even if a late reply never arrives.
        def remove(_future):
            with self.pending_lock:
                self.pending.pop(identifier, None)
        future.add_done_callback(remove)
        return future

    def reader(self):
        try:
            while True:
                line = self.input.readline(MAX_FRAME + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    break
                frame = json.loads(line)
                if frame.get("type") == "rpc_result":
                    with self.pending_lock:
                        item = self.pending.get(frame.get("id"))
                    if item is not None:
                        future = item[1]
                        try:
                            if frame.get("error"):
                                future.set_exception(ToolError(str(frame["error"])))
                            else:
                                future.set_result(frame.get("result"))
                        except concurrent.futures.InvalidStateError:
                            pass
                elif frame.get("type") in ("execute", "shutdown"):
                    # Never wait behind user code: stdin EOF remains observable.
                    try:
                        self.commands.put_nowait(frame)
                    except queue.Full:
                        break
                else:
                    break
        except (OSError, ValueError, TypeError):
            pass
        self.cleanup_children()
        if os.name == "posix" and os.getpgrp() == os.getpid():
            os.killpg(os.getpid(), signal.SIGKILL)
        os._exit(0)

    def cleanup_children(self):
        with self.children_lock:
            children = list(self.children)
        with self.tracked_lock:
            tracked = list(self.tracked)
        for process in tracked:
            if process._repl_registered:
                self.stop_child(process)
            elif process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1)
        for process in children:
            self.stop_child(process)

    @staticmethod
    def stop_child(process):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    def sh(self, cmd, *, check=True, timeout=120.0, cwd=None, env=None):
        """Bounded shell capture; default cwd follows Python os.chdir()."""
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("sh timeout must be a finite positive number")
        with self.children_lock:
            if len(self.children) >= 64:
                raise RuntimeError("Too many active sh processes (limit 64)")
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=os.path.expanduser(os.fspath(cwd)) if cwd else None,
                                   env={**os.environ, **env} if env else None,
                                   start_new_session=os.name == "posix")
        with self.children_lock:
            self.children.add(process)
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = False
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired("sh command", timeout)
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        target = buffers[key.data]
                        room = OUTPUT_BYTES - len(target)
                        target.extend(chunk[:room])
                        truncated |= len(chunk) > room
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            decoded_stdout = buffers["stdout"].decode("utf-8", "replace")
            decoded_stderr = buffers["stderr"].decode("utf-8", "replace")
            bounded_stdout = prefix(decoded_stdout, OUTPUT_BYTES)
            bounded_stderr = prefix(decoded_stderr, OUTPUT_BYTES)
            truncated |= bounded_stdout != decoded_stdout or bounded_stderr != decoded_stderr
            result = ShellResult(bounded_stdout,
                                 returncode=process.returncode,
                                 stderr=bounded_stderr,
                                 truncated=truncated)
            if check and not result.ok:
                raise ShellError(result)
            return result
        finally:
            # Also terminate background descendants after their shell exits.
            self.stop_child(process)
            process.stdout.close()
            process.stderr.close()
            with self.children_lock:
                self.children.discard(process)

    def evaluate(self, node, filename):
        compiled = compile(node, filename, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        async def invoke():
            value = eval(compiled, self.namespace)
            return await value if compiled.co_flags & 0x80 else value
        task = self.loop.create_task(invoke())
        try:
            return self.loop.run_until_complete(task)
        except BaseException:
            task.cancel()
            # Run cancellation cleanup on the same loop; parent enforces grace.
            with contextlib.suppress(BaseException):
                if not task.done():
                    self.loop.run_until_complete(task)
                else:
                    task.exception()
            raise

    def execute(self, frame):
        identifier = frame["id"]
        self.run_id = identifier
        started = time.monotonic()
        stdout = BoundedCapture(self, identifier, "stdout")
        stderr = BoundedCapture(self, identifier, "stderr")
        result = {"run_id": identifier, "success": True, "stdout": "", "stderr": "",
                  "return_value": None, "error": None, "elapsed_ms": 0,
                  "truncated": {"stdout": False, "stderr": False, "return": False},
                  "state": "preserved"}
        filename = f"<repl:{identifier}>"
        self.remember_source(identifier, frame["code"])
        baseline_tasks = asyncio.all_tasks(self.loop)
        baseline_handles = set(self.loop._scheduled) | set(self.loop._ready)
        baseline_threads = set(threading.enumerate())
        run_token = RUN_CONTEXT.set(identifier)
        fatal_background = False
        self.executing = True
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                tree = ast.parse(frame["code"], filename=filename, mode="exec")
                last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
                if tree.body:
                    self.evaluate(tree, filename)
                if last is not None:
                    # Store the trailing expression only briefly, without running repr.
                    target = ast.Name(id="__repl_result__", ctx=ast.Store())
                    assignment = ast.copy_location(ast.Assign(targets=[target], value=last.value), last)
                    self.evaluate(ast.fix_missing_locations(ast.Module(body=[assignment], type_ignores=[])), filename)
                    value = self.namespace.pop("__repl_result__", None)
                    if value is not None:
                        text, converted, shortened = preview(value)
                        result["return_value"] = text
                        result["truncated"]["return"] = shortened
                        if not shortened:
                            result["value"] = converted
            except BaseException as exc:
                result["success"] = False
                if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                    result["error"] = f"Execution interrupted ({identifier}); namespace preserved"
                else:
                    # Do not format helper stack frames or inspect namespace locals.
                    entries = traceback.StackSummary.extract(
                        traceback.walk_tb(exc.__traceback__), limit=-20, lookup_lines=False)
                    locations = []
                    for entry in entries:
                        if entry.filename.startswith("<repl:"):
                            locations.append(f'  File "{entry.filename}", line {entry.lineno}, in {entry.name}')
                            if entry.line:
                                locations.append("    " + prefix(entry.line.strip(), 500))
                    try:
                        message = prefix(str(exc), 4000)
                    except BaseException:
                        message = "exception message unavailable"
                    result["error"] = prefix("\n".join(locations + [f"{type(exc).__name__}: {message}"]), 10000)
            finally:
                owned_tasks = asyncio.all_tasks(self.loop) - baseline_tasks
                for task in owned_tasks:
                    task.cancel()
                if owned_tasks:
                    with contextlib.suppress(BaseException):
                        self.loop.run_until_complete(asyncio.wait(owned_tasks, timeout=0.25))
                    result["background_tasks_cancelled"] = len(owned_tasks)
                    if asyncio.all_tasks(self.loop) - baseline_tasks:
                        result.update(success=False, state="cleared", error="Background tasks refused cancellation; worker stopped and variables cleared")
                        fatal_background = True
                # Timers are not asyncio Tasks; retire run-owned delayed effects.
                handles = set(self.loop._scheduled) | set(self.loop._ready)
                for handle in handles - baseline_handles:
                    if handle._context.get(RUN_CONTEXT) == identifier:
                        handle.cancel()
                live_threads = [thread for thread in threading.enumerate()
                                if thread not in baseline_threads and thread not in self.executor._threads and thread.is_alive()]
                active_executor = self.executor.cancel_run(identifier)
                if live_threads or active_executor:
                    result.update(success=False, state="cleared", error="Cell left active background threads or executor work; worker stopped and variables cleared. Join threads before the cell ends; use mcp.acall for concurrent bridge calls.")
                    fatal_background = True
                self.executing = False
                self.namespace.pop("__repl_result__", None)
                self.namespace["mcp"] = MCPBridge(self)
                self.namespace["sh"] = self.sh
                self.namespace["checkpoint"] = self.checkpoint
                self.namespace["restore"] = self.restore
                self.namespace["repl_history"] = self.repl_history
                self.namespace["repl_source"] = self.repl_source
                with self.pending_lock:
                    pending = [future for run, future in self.pending.values() if run == identifier]
                for future in pending:
                    future.cancel()
                self.run_id = None
                RUN_CONTEXT.reset(run_token)
        result["stdout"], result["stderr"] = stdout.getvalue(), stderr.getvalue()
        result["truncated"].update(stdout=stdout.truncated, stderr=stderr.truncated)
        result["elapsed_ms"] = (time.monotonic() - started) * 1000
        self.send({"type": "result", "id": identifier, "result": result})
        if fatal_background:
            self.cleanup_children()
            os._exit(70)

    def interrupt(self, _signum, _frame):
        if self.executing:
            raise KeyboardInterrupt()

    def serve(self):
        signal.signal(signal.SIGINT, self.interrupt)
        threading.Thread(target=self.reader, name="worker-control", daemon=True).start()
        self.send({"type": "ready", "python_version": sys.version.split()[0], "pid": os.getpid()})
        try:
            while True:
                frame = self.commands.get()
                if frame["type"] == "shutdown":
                    break
                self.execute(frame)
        finally:
            self.cleanup_children()
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            if tasks:
                with contextlib.suppress(BaseException):
                    self.loop.run_until_complete(asyncio.wait(tasks, timeout=0.25))
            self.loop.close()


def main():
    worker = Worker()
    # Direct os.write(1,...) cannot corrupt private JSON framing.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    worker.serve()


if __name__ == "__main__":
    main()
