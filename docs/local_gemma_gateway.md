# Local Gemma-Compatible Model Gateway

## Overview

The gateway routes structured inference requests to a local Gemma-compatible runtime.
It can run with:

1. **`stub`** — deterministic mock responses (for tests, CI, and development)
2. **`ollama`** — local Ollama HTTP API (`ollama serve`)
3. **`llama_cpp`** — llama.cpp server (OpenAI-compatible)
4. **`vllm`** — vLLM OpenAI-compatible local API
5. **`lm_studio`** — LM Studio local server (OpenAI-compatible)

## Setup

### Stub (default — no GPU required)

```env
LOCAL_LLM_RUNTIME=stub
```

### Ollama

```bash
# Install Ollama: https://ollama.com
ollama pull gemma3:27b       # or gemma3:12b for smaller GPU
ollama serve
```

```env
LOCAL_LLM_RUNTIME=ollama
LOCAL_LLM_BASE_URL=http://localhost:11434
LOCAL_LLM_PRIMARY_MODEL=gemma3:27b
```

### llama.cpp

```bash
llama-server -m gemma-2b-it.gguf --port 8080
```

```env
LOCAL_LLM_RUNTIME=llama_cpp
LOCAL_LLM_BASE_URL=http://localhost:8080
```

### vLLM

```bash
python -m vllm.entrypoints.openai.api_server \
    --model google/gemma-3-27b-it --port 8000
```

```env
LOCAL_LLM_RUNTIME=vllm
LOCAL_LLM_BASE_URL=http://localhost:8000
```

### LM Studio

```
Install LM Studio: https://lmstudio.ai — load a model and start the local
server (Developer tab → Start Server). LM Studio exposes the same
OpenAI-compatible surface as llama.cpp/vLLM (/v1/chat/completions,
/v1/models), so it reuses the same dispatch code path.
```

```env
LOCAL_LLM_RUNTIME=lm_studio
LOCAL_LLM_BASE_URL=http://localhost:1234
# From a container, use http://host.docker.internal:1234 instead — "localhost"
# inside the container means the container itself, not the host running LM
# Studio, and would silently fall back to stub forever. LM Studio's server
# must also have network access enabled for non-127.0.0.1 clients.
LOCAL_LLM_PRIMARY_MODEL=qwen/qwen3.6-35b-a3b
LOCAL_LLM_FAST_MODEL=google/gemma-4-e4b
LOCAL_LLM_COORDINATOR_MODEL=qwen/qwen3.6-35b-a3b
```

## Concurrency Control

`LocalModelGateway` caps concurrent in-flight HTTP calls to the configured
runtime with `LOCAL_LLM_MAX_CONCURRENT_CALLS` (default `2`), enforced via a
`threading.BoundedSemaphore` (not `asyncio.Semaphore` — `generate()` runs
synchronously inside `ThreadPoolExecutor` worker threads spawned by
`AgentCouncilSupervisor`, not asyncio tasks). This is independent of
`AGENT_SCAN_CONCURRENCY`, which only bounds how many underlying-symbol
pipelines run concurrently, not how many of them can be waiting on an LLM
call at once.

A call that cannot acquire a slot within a short bounded wait (~20% of
`LOCAL_LLM_TIMEOUT_SECONDS`, capped at 3s) fails fast into the same safe
stub-fallback path used for every other failure, with
`failure_mode="concurrency_saturated"` — it never queues indefinitely.

This exists because a single local inference server cannot serve hundreds of
simultaneous requests: an attempt to run the AI council against Ollama under
real scan-cycle load (up to ~600 concurrent calls per cycle onto one model
instance) caused every call to time out, burned 192s+ of a 300s cycle budget,
and correlated with WebSocket feed drops in the same window (plausible
event-loop starvation). See `.env`'s "Local model gateway" comment block for
the full incident record.

## Safety Guarantees

- No broker credentials enter LLM prompts.
- Only structured JSON responses are accepted.
- Timeout and retry controls enforce hard deadlines.
- Failures return a safe HOLD stub — never crash the scan.
- LLM agents cannot create `OrderIntent` objects.

## Model Routing

Model IDs are whatever the configured runtime reports/expects — the table
below shows the stub placeholder names alongside a real LM Studio example.

| Role | Placeholder (stub) | LM Studio example |
|------|---------------------|--------------------|
| Primary (chief analyst / strategy review) | `gemma4-31b` | `qwen/qwen3.6-35b-a3b` |
| Coordinator (portfolio manager) | `gemma4-26b-moe` | `qwen/qwen3.6-35b-a3b` |
| Fast (bulk specialists) | `gemma4-e4b` | `google/gemma-4-e4b` |
