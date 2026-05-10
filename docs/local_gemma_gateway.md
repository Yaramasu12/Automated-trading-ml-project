# Local Gemma-Compatible Model Gateway

## Overview

The gateway routes structured inference requests to a local Gemma-compatible runtime.
It can run with:

1. **`stub`** — deterministic mock responses (for tests, CI, and development)
2. **`ollama`** — local Ollama HTTP API (`ollama serve`)
3. **`llama_cpp`** — llama.cpp server (OpenAI-compatible)
4. **`vllm`** — vLLM OpenAI-compatible local API

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

## Safety Guarantees

- No broker credentials enter LLM prompts.
- Only structured JSON responses are accepted.
- Timeout and retry controls enforce hard deadlines.
- Failures return a safe HOLD stub — never crash the scan.
- LLM agents cannot create `OrderIntent` objects.

## Model Routing

| Model ID | Role | Default |
|----------|------|---------|
| `gemma4-31b` | Chief analyst / strategy review | Primary |
| `gemma4-26b-moe` | Coordinator / portfolio manager | Coordinator |
| `gemma4-e4b` | Fast micro-agents | Specialists |
