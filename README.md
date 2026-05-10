# Agentic AI-Guided Evaluation Platform

An LLM evaluation platform that works as an MCP server in your IDE. An expert AI agent guides you through the entire evaluation process via natural conversation: describe what you want to evaluate, upload documents, and the agent handles dataset generation, judge configuration, execution, and analysis.

## Features

- **Expert agent interface** — The agent knows evaluation best practices, recommends criteria and validates configurations before execution. No config files or CLI expertise needed.
- **Jury system** — Multiple judges from different model families (e.g. Claude Sonnet, Nova Pro, Nemotron) each evaluate distinct aspects of every response — correctness, reasoning, completeness. Combining diverse judge families reduces self-preference bias, and aggregating weak signals from diverse judges and criteria produces stronger results than any single judge ([Verma et al., 2025](https://arxiv.org/abs/2502.20379), [Frick et al., 2025](https://arxiv.org/abs/2506.18203)).
- **Adaptable binary scoring** — Binary pass/fail per criteria rather than subjective numeric scales, shown to produce more reliable results across judges ([Chiang et al., 2025](https://arxiv.org/abs/2503.23339v2)). Criteria are tailored by the agent to what you're evaluating.
- **Document-grounded synthetic data** — Upload PDFs, knowledge bases, or product docs and generate QA pairs grounded in your actual content, reflecting real customer scenarios.
- **Agentic eval support** — Evaluate any agent calling Bedrock (Strands, LangChain, custom boto3) with zero code modification via OpenTelemetry instrumentation.

## Quick Start (MCP)

### Prerequisites

- Python 3.11+
- AWS credentials with Bedrock model access
- Claude Code, Cursor, Kiro, or any MCP-compatible IDE

### Install

```bash
git clone https://github.com/awslabs/llm-evaluation-system.git && cd llm-evaluation-system
uv pip install -e .
```

### Add to your IDE

**Claude Code** — add to `.claude/settings.json`:
```json
{
  "mcpServers": {
    "eval": {
      "command": "eval-mcp"
    }
  }
}
```

**Cursor / VS Code** — add to MCP settings:
```json
{
  "eval": {
    "command": "eval-mcp"
  }
}
```

**Kiro** — add to `.kiro/settings/mcp.json` (or your user-level Kiro MCP config):
```json
{
  "mcpServers": {
    "eval": {
      "command": "eval-mcp"
    }
  }
}
```

### Use

Just ask your AI assistant:

- "Evaluate my RAG pipeline on these documents"
- "Generate a QA dataset from this PDF"
- "Compare Claude Sonnet vs Nova Pro on my test cases"
- "Run an agent eval on my Strands agent"

The agent handles the rest.

### View Results

```bash
eval-mcp view
```

Opens the comparison viewer at http://localhost:4001.

## Team Sharing (S3)

Share datasets, judges, configs, and eval results across your team via a shared S3 bucket. No servers needed.

### Setup

```bash
eval-mcp config set bucket my-team-evals
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
- Every list/read auto-pulls from S3 first (debounced, ~100ms) so your local state mirrors S3
- `eval-mcp share my-project` → promote your stuff to a shared project prefix
- `eval-mcp sync` → manual reconcile (used after long offline periods or on a fresh laptop)

### Create the bucket

```bash
cd infra/modules/eval-logs-bucket
terraform init
terraform apply -var="bucket_name=my-team-evals"
```

## Self-host the MCP

To run `eval-mcp` on a shared host (EC2, EKS, AgentCore, anywhere Python runs) so a team or CI pipeline points at one HTTP endpoint, see [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md). A `Dockerfile` is included at the repo root.

This is the lightweight path — just the eval engine + viewer. For the full multi-user web app with chat, auth, and per-user isolation, see [Deploy Full Platform on EKS](#deploy-full-platform-on-eks) below.

## Agent Evaluation

Evaluate any agent that calls Bedrock via boto3 — no code modification needed.

The platform uses OpenTelemetry to intercept all Bedrock API calls at the botocore layer. Your agent runs unmodified; the instrumentation captures every LLM interaction (messages, tool calls, token usage) and feeds them into Inspect AI for scoring.

```python
# Your agent — completely unmodified
def my_agent(prompt):
    client = boto3.client("bedrock-runtime")
    response = client.converse(modelId="us.anthropic.claude-sonnet-4-6", ...)
    return response

# Eval wraps it transparently
with bedrock_capture():
    result = my_agent("What is 2+2?")
```

Works with Strands, LangChain, CrewAI, Claude Agent SDK, or any custom agent using boto3.

## Deploy Full Platform on EKS

For multi-user deployment with authentication and a polished web UI, run the full platform on EKS:

```bash
./deploy.sh
```

The script auto-installs Terraform, kubectl, and Helm, then deploys the complete platform with Cognito auth, CloudFront, WAF, and per-user isolation. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for details.

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

## Local Development

For working on the platform itself with hot reload (full web UI in Docker Compose), see [local/README.md](local/README.md).

## Acknowledgments

This platform is built on [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) by the UK AI Security Institute, an open-source framework for large language model evaluations.

## Legal Disclaimer

Sample code, software libraries, command line tools, proofs of concept, templates, or other related technology are provided as AWS Content or Third-Party Content under the AWS Customer Agreement, or the relevant written agreement between you and AWS (whichever applies). You should not use this AWS Content or Third-Party Content in your production accounts, or on production or other critical data. You are responsible for testing, securing, and optimizing the AWS Content or Third-Party Content, such as sample code, as appropriate for production grade use based on your specific quality control practices and standards. Deploying AWS Content or Third-Party Content may incur AWS charges for creating or using AWS chargeable resources, such as running Amazon EC2 instances or using Amazon S3 storage.
