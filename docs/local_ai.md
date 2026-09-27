# Local AI with Ollama (free, offline, no API key)

The AI assistant can use a model running on your own computer through
[Ollama](https://ollama.com). Nothing about your board leaves the PC, and no key is
needed. The app talks to Ollama's OpenAI-compatible API at
`http://localhost:11434/v1` with the same adapter and the same local validation as
cloud models (the AI still only proposes; the deterministic router and checks decide).

## Setup (Windows)
1. Double-click `setup_ollama.bat` (installs Ollama with winget if missing; start
   Ollama once from the Start menu, then run the file again). It downloads
   `qwen2.5:7b` (~4.7 GB, needs ~16 GB RAM) — for 8 GB RAM run
   `setup_ollama.bat qwen2.5:3b`.
2. Or in the app: **AI ▸ Set Up Local AI (Ollama)…** → pick a model → *Download model*
   → *Use this model* → *Test*.
3. Or from a command prompt: `python -m pcbrouter --setup-ollama qwen2.5:7b`.

## What to expect
* Speed: on a laptop CPU/iGPU a 7B model answers in roughly 10 s–2 min; the first
  answer is slower (the model loads). The request timeout for this profile is 300 s.
* Quality: small local models follow the JSON schema less reliably than large cloud
  models; invalid answers are rejected by local validation (never applied), and the
  adapter falls back json_schema → json_object → plain automatically.
* Reasoning ("thinking") models (e.g. the qwen3 family) narrate their thoughts
  before answering, which multiplies generation time on CPU past any timeout. The
  app disables reasoning for structured routing commands and badges thinking
  models in the model list — but for local use a non-thinking model
  (`qwen2.5:7b`, `qwen2.5:3b`) answers far sooner.
* Very large context windows (100k+ tokens) need a matching KV cache in RAM:
  Ollama may fail to load such a model on 8–16 GB machines. The model list shows
  each model's context size; if loading fails, pick a smaller-context model.
* Ollama on Windows mostly runs models on the CPU; Iris Xe acceleration in Ollama is
  not guaranteed (that is separate from the router's dpnp GPU path).
* Only `localhost` Ollama addresses are accepted by the setup.

Tested here against a mock Ollama HTTP server (real sockets, real OpenAI SDK); a real
Ollama server/model could not be run in the development container (download blocked).
