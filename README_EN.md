# AgentNexus (智枢)

[简体中文](README.md) | **English**

AgentNexus is a browser-based agent work platform. It provides one place to manage models, agents, Skills, MCP tools, task history, and generated files. Users can start a conversation directly or configure workflows for their own work scenarios.

Its main capabilities include:

- Standard mode: one agent understands the conversation context and matches enabled Skills and MCP tools.
- Expert mode: the user can choose an enabled expert team or let the platform match one to the task. Team members work independently in parallel, and the supervisor consolidates their work.
- Model management: OpenAI and OpenAI-compatible Chat Completions endpoints, with API keys supplied through environment variables or entered directly in the UI.
- Skill management: create, edit, import, export, and install Skill packages from HTTPS download links.
- Tool integration: local stdio MCP, remote Streamable HTTP MCP, and regular HTTP tools.
- File handling: extract text from common text files, Office documents, and PDFs, then generate downloadable Word, PDF, Excel, Markdown, HTML, or PPTX files. PPTX uses the bundled Python generator by default; an external Artifact Tool is optional.
- Task execution: an event stream reports the plan, selected Skills and model, tool calls, file generation, and final validation.
- Memory and automation: retain explicit long-term preferences or trigger recurring tasks on a schedule or through a webhook.

The built-in offline model requires no API key and is intended for checking the UI, task flow, and file pipeline. It is not a general-purpose language model. Configure a real model before using AgentNexus for content creation, complex analysis, or tool planning.

The current web UI is primarily in Chinese. This guide shows the corresponding Chinese menu names where needed.

## Requirements

- Python 3.12
- Node.js 22 or a compatible version for frontend script checks. Node.js is only needed when choosing an external Artifact Tool for PPTX generation.
- SQLite (included with Python; no separate installation is needed)

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
./start-local.sh
```

`.env.local` stores machine-specific network switches, host allowlists, and optional environment-variable credentials. Git ignores this file, so it is not uploaded to the repository. Online models require `APP_ALLOW_OUTBOUND_NETWORK=true` and the model Base URL hostname in `APP_MODEL_HOST_ALLOWLIST`. If the API key is entered directly in the UI, it does not need to be duplicated in `.env.local`.

For the built-in offline model only, you may omit `.env.local` and run `./start-local.sh`; AgentNexus then starts with its network-disabled secure defaults.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env.local
uvicorn app.main:app --env-file .env.local --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) in a browser. The health-check endpoint is [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health).

Do not open `web/index.html` directly with `file://`. A static page cannot save models, upload files, or run tasks.

## Docker

Compose now includes the API, Redis, and an independent Worker. Without `REDIS_URL`, local startup retains in-process execution. For the small-team deployment configuration and current acceptance status, see [the release runbook](docs/SECOND_RELEASE_RUNBOOK.md) and [the release audit](docs/SECOND_RELEASE_AUDIT.md).

```bash
docker compose up --build
```

The current Dockerfile is Python-based. The bundled Python PPTX generator does not require Docker, Node.js, or npm; Node.js and an approved generation component are only needed when choosing the external Artifact Tool path.

Compose binds the service to `127.0.0.1:8000` on the host and disables application-level outbound network access by default. To use online models or external tools, set `APP_ALLOW_OUTBOUND_NETWORK=true` and enable the relevant capability-specific switches, then recreate the container.

## Testing

After installing the runtime dependencies, run the full regression suite from the repository root:

```bash
node --check web/diagnostics-routing.js
node --check web/app.js
python -m compileall -q app
python -m unittest discover -s tests -p 'test_*.py' -v
```

Tests use temporary databases and mocked services by default. They do not require model API keys and do not call external models, MCP services, or web-search providers. Frontend smoke tests also fetch the homepage and JavaScript assets through FastAPI's static file service, verifying diagnostics routing load order, cache-version consistency, and that network diagnostics target the web-search MCP instead of weather or local filesystem tools.

GitHub Actions runs the same frontend script checks, application compilation, and regression tests on Python 3.12 and 3.13 with outbound network, remote installation, and external-tool switches disabled.

## Model configuration

Open “模型设置” (Model Settings), select “添加模型” (Add Model), and enter a model name, Base URL, and credential method. The supported providers are:

- `openai`: an OpenAI Chat Completions-compatible endpoint.
- `openai_compatible`: a service that exposes a `/v1/chat/completions`-compatible endpoint.

To use an environment variable, define the key in the process that starts AgentNexus:

```bash
cp .env.example .env.local
# Edit .env.local:
# OPENAI_API_KEY=your-api-key
# APP_ALLOW_OUTBOUND_NETWORK=true
# APP_MODEL_HOST_ALLOWLIST=api.openai.com
./start-local.sh
```

In the UI, select “环境变量” (Environment Variable) and enter `OPENAI_API_KEY`. Base URL must be the provider's OpenAI-compatible API root, such as `https://api.openai.com/v1`; AgentNexus appends `/chat/completions` when sending a request. Set `APP_MODEL_HOST_ALLOWLIST` to the allowed model hostnames, separated by commas and without schemes or paths.

You may instead select “直接填写 API Key” (Enter API Key Directly). The key is encrypted with a local machine key before being stored in SQLite, and the UI never returns its plaintext value. This is a single-machine storage mechanism, not a replacement for a production Secret Manager or KMS.

After saving, the model appears under configured models and in the workspace model selector. Test the connection before running a task with it.

## Getting started

1. Configure and test a real model in “模型设置” (Model Settings).
2. Return to the workspace and choose standard or expert mode above the input box.
3. Select the model for this task. In standard mode, you can also select an agent.
4. Add any supporting files before sending the request.
5. Enter the goal and send it. The panel on the right shows execution progress and the Skills, model, and tools actually used.
6. Download generated files from the response or find them later under “产物” (Artifacts).

The selected mode remains active until you switch it manually. Standard mode does not automatically become expert mode when a task is complex. Expert mode requires an expert team to be created and enabled first under “专家团” (Expert Teams).

Try one of these prompts:

```text
Using the material I uploaded, summarize the main conclusions, risks, and next actions, then generate a Word report for download.
```

```text
Review this release plan from product, engineering, and security perspectives, and provide clearly prioritized recommendations.
```

## Skills and MCP

A Skill describes when it applies, how to execute the work, which tools it depends on, and what output it should produce. Under “技能中心” (Skills), you can create a Skill or import one from a `SKILL.md` file, ZIP package, or public HTTPS download link. Once installed, a Skill can be inspected, disabled, edited, or exported from the Skill list.

MCP integrations are managed under “工具接入” (Tool Integrations). AgentNexus can import common `mcpServers` JSON, or you can configure a local stdio service, remote Streamable HTTP service, or regular HTTP tool manually. External processes and network tools are disabled by default; the deployer must explicitly enable them through environment variables and configure allowlists. After adding a service, bind its service ID to the relevant agent.

Local stdio MCP requires at least `APP_ALLOW_STDIO_MCP`. List only reviewed executables in `APP_STDIO_COMMAND_ALLOWLIST`; absolute paths are recommended for shared deployments. Remote MCP and regular HTTP tools also require the outbound-network master switch and their capability-specific switches. Restrict destinations with `APP_REMOTE_HOST_ALLOWLIST`. Allowlists contain hostnames only, separated by commas.

The MCP page masks common secret fields in the UI, but service configurations are still stored as JSON in SQLite and do not use the encrypted storage mechanism used for model keys. Do not place tokens directly in the configuration. Prefer `${ENV_NAME}` references, which AgentNexus resolves from its server process environment at call time, and protect the database as sensitive runtime data.

For complete installation methods, examples, and security controls, see the [Platform Guide (Chinese)](docs/PLATFORM_GUIDE.md).

## Files and artifacts

File uploads are enabled by default. Extractable text from TXT, Markdown, CSV, JSON, YAML, common source-code files, DOCX, XLSX, PPTX, and PDF files can be added to task context. Scanned PDFs are not processed with OCR, and legacy Office formats are outside the default parsing scope.

AgentNexus has built-in generation for DOCX, PDF, XLSX, Markdown, HTML, and PPTX through `python-pptx`. In the UI, open “模型设置 → 文档交付 → PPTX 配置向导” and choose the bundled Python generator. The equivalent setting is:

```bash
export APP_PPTX_GENERATOR='python'
```

If the deployment already has an approved Artifact Tool, the optional external path can be configured with Node.js and its entry point:

```bash
export APP_NODE_BINARY='node'
export APP_ARTIFACT_TOOL_ENTRYPOINT='/absolute/path/to/artifact_tool.mjs'
```

The project does not install or execute Artifact Tool from arbitrary URLs. A missing or invalid PPTX configuration is reported in the conversation with an actionable next step and does not affect the other document formats.

Files are stored in runtime directories under `data/` and accessed through controlled preview and download endpoints. Databases, uploaded files, generated files, and local machine keys are runtime data and must not be committed to the repository.

## Important limitations

The implementation now includes optional login, administrator/member roles, workspace membership and resource checks, and Redis Streams/Worker execution. Authentication remains disabled for local development by default; the small-team configuration enables it. This does not provide enterprise multi-tenant RBAC, a sandbox per task, or a highly available cluster. Do not expose it to an untrusted public network without completing deployment validation.

Installing a Skill does not execute scripts contained in its package. A local stdio MCP service starts a process on the AgentNexus server, not on the browser user's computer. Restrict commands, remote hosts, and tool permissions before enabling these capabilities.

`APP_ALLOW_OUTBOUND_NETWORK` is disabled by default and acts as the master switch for online models, weather, web search, remote MCP, HTTP tools, remote policies, and installation from download links. It does not restrict network access initiated independently by stdio child processes. A strictly offline deployment must also enforce outbound restrictions through containers, host firewalls, or network policies.

## Project structure

```text
app/
  main.py                  FastAPI entry point and HTTP/SSE endpoints
  db.py                    SQLite storage
  builtin_skill_catalog.py Small catalog of Skills installable by confirmation in chat
  builtin_skills/          Skills loaded with the platform
  services/                Task, model, Skill, MCP, expert-team, memory, and artifact services
web/                       Browser UI
examples/                  Configuration examples for manual installation
docs/                      Usage and architecture documentation
data/                      Local runtime data (not committed)
```

See the [Architecture Guide (Chinese)](docs/ARCHITECTURE.md) for implementation details. Refer to [.env.example](.env.example) for the available environment variables. Restart the AgentNexus process after changing environment variables.

## Contributing

Bug reports and pull requests are welcome through [Issues](https://github.com/YangFW/AgentNexus/issues). Before submitting a change, make sure it does not include `.env` files, API keys, databases, uploaded files, generated files, or other local runtime data. In a pull request, describe the purpose of the change, how to use it, and how it was verified.

## References and acknowledgements

AgentNexus is an independent open-source project. Its protocol implementation and infrastructure use or reference the following public specifications and projects:

- [Model Context Protocol](https://modelcontextprotocol.io/specification/latest) and the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk): MCP service integration and tool invocation.
- [OpenAI API Reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create): the Chat Completions-compatible model interface.
- [FastAPI](https://fastapi.tiangolo.com/) and [Uvicorn](https://www.uvicorn.org/): HTTP APIs, static UI hosting, and event streaming.
- [SQLite](https://www.sqlite.org/docs.html): local configuration and task-data storage.
- [Open-Meteo](https://open-meteo.com/en/docs): data API for the built-in weather tool.

These names are used only to identify compatible protocols, dependencies, or data sources. They do not imply endorsement of AgentNexus by the corresponding projects or organizations. Third-party components and external services remain subject to their own licenses and terms of service.

## Citation

If AgentNexus is useful in your project or research, please consider giving it a Star ⭐. To cite the project in a paper, report, or other work, use:

```bibtex
@software{YangFW_AgentNexus_2026,
  author  = {{YangFW}},
  title   = {AgentNexus},
  year    = {2026},
  url     = {https://github.com/YangFW/AgentNexus},
  license = {MIT}
}
```

The repository also provides a standard [`CITATION.cff`](CITATION.cff) for GitHub and citation-management tools.

## License

The original AgentNexus source code is released under the [MIT License](LICENSE).
