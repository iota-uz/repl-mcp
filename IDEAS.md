# REPL MCP Enhancement Ideas

This document captures brainstorming ideas for enhancing the Python REPL MCP. These range from practical to experimental. The goal is to give LLMs programmatic superpowers when working with codebases and external tools.

---

## Inspiration

- **RLM Paper** (arxiv.org/abs/2512.24601): Recursive Language Models - LLMs exploring large contexts via Python REPL, calling themselves recursively on sub-problems
- **ysz/recursive-llm**: Reference implementation storing context as variables, not in prompts

Key insight from RLM: Context stored externally + programmatic exploration + recursive LLM calls = handling 100k+ tokens efficiently.

---

## Core Capabilities (Phase 1 - In Progress)

### Workspace File Access
**Problem:** Can't read/write files programmatically from REPL code.
**Idea:** `workspace.read()`, `workspace.write()`, `workspace.glob()` with sandboxed access to working directory.

### Git Primitives
**Problem:** Parsing git CLI output is painful, lose structure.
**Idea:** `git.log()`, `git.diff()`, `git.blame()`, `git.status()` returning structured data (Pydantic models).

### AST Utilities
**Problem:** Grep finds text, not code structure. Can't distinguish function calls from definitions.
**Idea:** `ast_utils.find_function_calls()`, `ast_utils.find_class_definitions()`, `ast_utils.dependency_graph()` using Python's ast module.

### Data Injection
**Problem:** Can't easily pass data from Claude's context into REPL.
**Idea:** `execute_python(code, inject={"my_data": [...]})`

---

## Context Management

### Pipe Pattern (High Priority)
**Problem:** Fetching CI logs, PR comments, API responses floods context with noise. Context rot degrades performance.
**Idea:** Pipe large data through a cheap/fast LLM to extract relevant bits before it hits main context.
```
Large Data → Filter LLM (Haiku) → Relevant Excerpt → Main LLM (Opus/Sonnet)
```
Like Unix pipes but semantic: `ci_logs | pipe("find root cause")`

### Specialized Filters
**Problem:** Common filtering patterns repeated manually.
**Idea:** Pre-built filters: `filter.errors()`, `filter.root_cause()`, `filter.actionable()`, `filter.json_path()`, `filter.code_blocks()`

### Smart Log Filtering
**Problem:** CI logs are 50k tokens, only 500 matter.
**Idea:** `ci.get_logs(run_id, filter="errors_only")` or `ci.summarize(run_id)` using LLM extraction.

### Output Truncation with Summary
**Problem:** Large outputs fill context, but simple truncation loses important info.
**Idea:** When output exceeds threshold, auto-summarize instead of truncate. Keep structure, lose verbosity.

---

## Session & Memory

### State Preservation
**Problem:** After compaction or session resume, lose track of what was being worked on, key findings, next steps.
**Idea:** `state.save({"task": "...", "findings": [...], "next_steps": [...]})` and `state.load()` to checkpoint/restore working state.

### Persistent Memory
**Problem:** Learn things about a codebase (quirks, patterns, decisions), forget them next session.
**Idea:** `memory.store("auth_quirk", "JWT validated in middleware")`, `memory.recall("auth")`. Persistent across sessions.

### Auto-Checkpoint
**Problem:** Forget to save state before context gets too long.
**Idea:** `state.auto_checkpoint(interval_tokens=50000)` - periodically capture working state.

### Skill/Tool Reminders
**Problem:** Have capabilities (skills, MCP tools) but forget to use them.
**Idea:** Proactive reminders when context suggests a skill would help. "You have /review-pr skill available."

---

## LLM Orchestration

### LLM Sub-Agents
**Problem:** Single perspective, single context window, can't delegate.
**Idea:** Call other LLM instances for sub-tasks: `llm.explore("find auth middleware")`, `llm.review(code, focus="security")`. Use Claude CLI print mode or Codex CLI.

### Multi-Perspective Design
**Problem:** One way of thinking, no pushback on ideas.
**Idea:** Get multiple approaches: `llm.parallel([{"persona": "minimalist"}, {"persona": "performance_engineer"}])`, then synthesize.

### Adversarial Review
**Problem:** Don't catch my own blind spots.
**Idea:** Have separate LLM actively try to break code: `llm.attack(code, focus="edge_cases")`.

### Recursive Exploration (RLM-style)
**Problem:** Large context can't fit in window.
**Idea:** Store context as variable, explore via code, recursively call LLM on sub-sections. `recursive_llm(sub_query, sub_context)`.

---

## API & Testing

### REST Debugging Toolkit
**Problem:** Debugging API calls is trial-and-error curl commands. Can't trace request→response→error easily.
**Idea:** `http.trace("POST", url, body)` showing request, response, timing, issues, suggestions.

### GraphQL Debugging
**Problem:** GraphQL errors are nested, schema validation is separate, introspection needed.
**Idea:** `gql.introspect()`, `gql.validate(query)`, `gql.trace(mutation, variables)` with structured error extraction.

### Session Flow Testing
**Problem:** Auth flows span multiple requests, state carries over, hard to debug.
**Idea:** `session.flow([step1, step2, step3])` with variable extraction between steps, shows where flow breaks.

### Browser Testing Helpers
**Problem:** Playwright exists but still hard to use effectively, can't easily see what's happening.
**Idea:** Higher-level abstractions, automatic screenshot capture, visual state inspection.

---

## Code Intelligence

### Universal AST (Multi-Language)
**Problem:** Real codebases are TypeScript, Go, Rust, not just Python.
**Idea:** Use tree-sitter for language-agnostic code analysis. `code.find_function("handleAuth", lang="typescript")`.

### Semantic Code Search
**Problem:** Grep finds text patterns, not semantic meaning. Can't search "code that handles auth failures".
**Idea:** LLM-powered semantic search over codebase, understanding intent not just syntax.

### Cross-Language Analysis
**Problem:** Frontend calls backend, need to trace across language boundaries.
**Idea:** `code.find_api_endpoints("src/")` working for Express, FastAPI, Gin, etc.

### Codebase Historian
**Problem:** Code exists but don't know why. "Why is there a 500ms sleep here?"
**Idea:** Analyze git history, PR discussions, commit messages to explain code evolution.

---

## Quality & Verification

### Test Oracle
**Problem:** Write tests but don't know if they're good tests, would they catch real bugs?
**Idea:** LLM generates edge case tests: `test_oracle.generate(code, strategy="boundary_values")`.

### Mutation Testing Integration
**Problem:** Tests pass but might not catch real bugs.
**Idea:** `quality.mutation_test("src/auth.py")` - run mutations, see which survive.

### Specification Verification
**Problem:** Code might not match what docstring/spec says.
**Idea:** `verify.against_spec(code, spec="Users charged once per billing cycle")` - find discrepancies.

### Type Inference
**Problem:** Untyped Python code, want type safety without manual annotation.
**Idea:** `types.infer("src/utils.py", confidence_threshold=0.9)` - suggest type annotations.

---

## Debugging & Profiling

### Runtime Inspection
**Problem:** Can't set breakpoints, inspect variables at runtime.
**Idea:** `debugger.trace("src/main.py", "process_request")` showing call graph, arguments, return values.

### Performance Profiling
**Problem:** Don't know if code is fast or slow without benchmarking.
**Idea:** `debugger.profile("tests/test_perf.py")` showing hot spots, memory usage, timing.

### Structured Error Analysis
**Problem:** Stack traces full of framework noise, hard to find root cause.
**Idea:** `errors.root_cause(trace)` - filter to first error in user code, skip framework internals.

---

## Workflow & Productivity

### Diff/Patch Generation
**Problem:** Want to generate changes programmatically, review before applying.
**Idea:** `diff_utils.create_patch(original, modified)` - returns unified diff.

### Batch File Operations
**Problem:** Processing many files requires many tool calls.
**Idea:** Process multiple files in single REPL execution, build up results programmatically.

### Result Caching
**Problem:** Expensive MCP tool calls repeated unnecessarily.
**Idea:** Cache layer for tool results: `@cached(ttl=300)` decorator pattern.

---

## Meta / Experimental

### Self-Improving Workflows
**Problem:** Manual iteration on code quality.
**Idea:** `review_loop(code)` - generate code, review, fix issues, verify fix, repeat until clean.

### Inference-Time Scaling
**Problem:** Some problems need more "thinking" than others.
**Idea:** Trade compute for quality - use recursive exploration, multiple perspectives, verification steps for hard problems.

### Post-Compaction Recovery
**Problem:** After context compaction, become "lazy" or forget important context.
**Idea:** Detect compaction, auto-load checkpointed state, re-inject critical context.

---

## External Tool Integration Notes

Some ideas overlap with existing tools:
- **Code review**: CodeRabbit, GitHub Copilot review
- **Semantic search**: Explore agent (imperfect but exists)
- **Memory**: Skills, CLAUDE.md, /memory commands
- **Browser testing**: Playwright MCP, playwright-cli skill

Focus on gaps these don't cover well, or integration layers that compose them.

---

## Priority Assessment

**Highest ROI (Simple + Impactful):**
1. Pipe pattern for context filtering
2. State save/load
3. Workspace + Git + AST (in progress)

**High Value but More Effort:**
4. API debugging toolkit
5. LLM sub-agents via CLI
6. Universal code intelligence

**Experimental / Research:**
7. Multi-perspective design
8. Semantic code search
9. Self-improving workflows

---

## Next Steps

1. Complete Phase 1 (workspace, git, ast)
2. Test in production project to validate usefulness
3. Implement pipe pattern if context rot is real problem
4. Add state preservation if session continuity is issue
5. Revisit ideas after real-world usage feedback
