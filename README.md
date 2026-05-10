<!-- mcp-name: io.github.awslabs/llm-evaluation-system -->

# Agentic AI-Guided Evaluation Platform

An LLM evaluation platform that plugs into your IDE as an MCP server. Describe what you want to evaluate — the AI assistant handles dataset generation, judge configuration, execution, and analysis through natural conversation.

## Features

- **Expert agent interface** — The agent knows evaluation best practices, recommends criteria and validates configurations before execution. No config files or CLI expertise needed.
- **Jury system** — Multiple judges from different model families (e.g. Claude Sonnet, Nova Pro, Nemotron) each evaluate distinct aspects of every response — correctness, reasoning, completeness. Combining diverse judge families reduces self-preference bias, and aggregating weak signals from diverse judges and criteria produces stronger results than any single judge ([Verma et al., 2025](https://arxiv.org/abs/2502.20379), [Frick et al., 2025](https://arxiv.org/abs/2506.18203)).
- **Adaptable binary scoring** — Binary pass/fail per criteria rather than subjective numeric scales, shown to produce more reliable results across judges ([Chiang et al., 2025](https://arxiv.org/abs/2503.23339v2)). Criteria are tailored by the agent to what you're evaluating.
- **Document-grounded synthetic data** — Upload PDFs, knowledge bases, or product docs and generate QA pairs grounded in your actual content, reflecting real customer scenarios.
- **Agentic eval support** — Evaluate any agent calling Bedrock (Strands, LangChain, custom boto3) with zero code modification via OpenTelemetry instrumentation.

## Quick Start

### Prerequisites

- AWS credentials with Bedrock model access
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installed
- Claude Code, Cursor, Kiro, VS Code, or any MCP-compatible IDE

### Install

Pick your IDE and paste / click.

**Claude Code** — one CLI command:
```bash
claude mcp add-json eval '{"type":"stdio","command":"uvx","args":["--from","llm-evaluation-system","eval-mcp"]}' --scope user
```

**Cursor** — one-click deeplink: [Install eval-mcp in Cursor](cursor://anysphere.cursor-deeplink/mcp/install?name=eval&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyItLWZyb20iLCJsbG0tZXZhbHVhdGlvbi1zeXN0ZW0iLCJldmFsLW1jcCJdfQ==)

**Kiro** — add to `~/.kiro/settings/mcp.json`:
```json
{
  "mcpServers": {
    "eval": {
      "command": "uvx",
      "args": ["--from", "llm-evaluation-system", "eval-mcp"]
    }
  }
}
```

**Codex CLI** — add to `~/.codex/config.toml`, then restart Codex:
```toml
[mcp_servers.eval]
command = "uvx"
args = ["--from", "llm-evaluation-system", "eval-mcp"]
```

**VS Code** (with GitHub Copilot MCP) — one CLI command:
```bash
code --add-mcp '{"name":"eval","command":"uvx","args":["--from","llm-evaluation-system","eval-mcp"]}'
```

### Use

Ask your AI assistant:

- "Evaluate my RAG pipeline on these documents"
- "Generate a QA dataset from this PDF and compare Claude Sonnet vs Nova Pro"
- "Run an agent eval on `./my_agent.py`"
- "Compare these three prompt templates"

The agent picks the right eval mode, auto-generates whatever's missing (dataset, judge, criteria), runs it, and gives you a PDF report.

### View Results

```bash
uvx --from llm-evaluation-system eval-mcp view
```

Opens the comparison viewer at http://localhost:4001. The viewer auto-opens after every eval run — this command is only for re-opening past results.

## Team Sharing (S3)

Share datasets, judges, configs, and eval results across your team via a shared S3 bucket. No servers needed.

### Setup

```bash
uvx --from llm-evaluation-system eval-mcp config set bucket my-team-evals
```

User identity is auto-detected from your AWS credentials. Projects are auto-discovered from the bucket.

### How it works

```
s3://my-team-evals/
  users/alice/            ← Alice's evals, datasets, judges, configs (auto-replicated on every write)
  users/bob/              ← Bob's
  projects/project-alpha/ ← shared team evals
  projects/project-beta/  ← shared team evals
```

- Every write (eval result, dataset, judge, config, PDF report) auto-replicates to `users/{you}/` in the background
- Every list/read auto-pulls from S3 first (debounced) so your local state mirrors S3
- `eval-mcp share my-project` → promote your stuff to a shared project prefix
- `eval-mcp sync` → manual reconcile (used after long offline periods or on a fresh laptop)

### Create the bucket

One person on the team runs this once:

```bash
git clone https://github.com/awslabs/llm-evaluation-system.git
cd llm-evaluation-system/infra/modules/eval-logs-bucket
terraform init
terraform apply -var="bucket_name=my-team-evals"
```

## Agent Evaluation

Evaluate any agent that calls Bedrock via boto3 — no code modification needed.

OpenTelemetry intercepts Bedrock API calls at the botocore layer. Your agent runs unmodified; the instrumentation captures every LLM interaction (messages, tool calls, token usage) and feeds them into Inspect AI for scoring.

```python
# Your agent — completely unmodified
def my_agent(prompt):
    client = boto3.client("bedrock-runtime")
    response = client.converse(modelId="us.anthropic.claude-sonnet-4-6", ...)
    return response
```

Works with Strands, LangChain, CrewAI, Claude Agent SDK, or any custom agent using boto3. Just point the agent at the eval:

> Evaluate the agent at `./my_agent.py` on 10 test cases.

The AI assistant analyzes the agent code, generates test cases, designs pipeline stages (routing → tool selection → argument quality → final output), runs the eval, and returns a scored report.

## Deploy Full Platform on EKS

For a multi-user web app with Cognito auth, chat UI, and per-user isolation, the repo also ships an EKS deployment. This is the heavyweight path — for most users the MCP above is enough.

```bash
./deploy.sh
```

The script auto-installs Terraform, kubectl, and Helm, then deploys the complete platform (Cognito auth, CloudFront, WAF, per-user isolation).

### User Management

```bash
./manage-users.sh create user@example.com
./manage-users.sh list
./manage-users.sh delete user@example.com
```

### Teardown

```bash
./destroy.sh
```

Architecture details, OIDC config, Helm values, and manual deployment steps: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Contributing / Local Development

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for how to clone, run from source, rebuild the viewer frontend, and contribute.

## Acknowledgments

Built on [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) by the UK AI Security Institute.

## Legal Disclaimer

Sample code, software libraries, command line tools, proofs of concept, templates, or other related technology are provided as AWS Content or Third-Party Content under the AWS Customer Agreement, or the relevant written agreement between you and AWS (whichever applies). You should not use this AWS Content or Third-Party Content in your production accounts, or on production or other critical data. You are responsible for testing, securing, and optimizing the AWS Content or Third-Party Content, such as sample code, as appropriate for production grade use based on your specific quality control practices and standards. Deploying AWS Content or Third-Party Content may incur AWS charges for creating or using AWS chargeable resources, such as running Amazon EC2 instances or using Amazon S3 storage.
