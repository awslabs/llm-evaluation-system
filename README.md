# Agentic AI-Guided Evaluation Platform

A web-based LLM evaluation platform deployed on AWS. Access it through a browser — each user gets their own isolated workspace. An expert AI agent guides you through the entire evaluation process via natural conversation: upload your documents, describe what you want to evaluate, and the agent handles dataset generation, judge configuration, execution, and analysis.

## Features

- **Expert agent interface** — The agent knows evaluation best practices, recommends criteria and validates configurations before execution. No config files or CLI expertise needed.
- **Jury system** — Multiple judges from different model families (e.g. Claude Sonnet, Nova Pro, Llama) each evaluate distinct aspects of every response — correctness, reasoning, completeness. Combining diverse judge families reduces self-preference bias, and aggregating weak signals from diverse judges and criteria produces stronger results than any single judge ([Verma et al., 2025](https://arxiv.org/abs/2502.20379), [Frick et al., 2025](https://arxiv.org/abs/2506.18203)).
- **Adaptable binary scoring** — Binary pass/fail per criteria rather than subjective numeric scales, shown to produce more reliable results across judges ([Chiang et al., 2025](https://arxiv.org/abs/2503.23339v2)). Criteria are tailored by the agent to what you're evaluating.
- **Document-grounded synthetic data** — Upload PDFs, knowledge bases, or product docs and generate QA pairs grounded in your actual content, reflecting real customer scenarios.
- **Agentic eval support** — Evaluate agent systems via Inspect AI's `sandbox_agent_bridge` (Strands, LangChain, any OpenAI-compatible framework).
- **Multi-tenant isolation** — Each user gets isolated data directories. Eval results stored as immutable `.eval` log files.
- **Integrated viewer** — Inspect AI's evaluation viewer embedded directly in the platform.

## Prerequisites

- AWS CLI configured with credentials
- An AWS account with Bedrock model access enabled

That's it. The deploy script auto-installs Terraform, kubectl, and Helm locally.

## Deploy

```bash
git clone https://github.com/awslabs/llm-evaluation-system.git && cd llm-evaluation-system
./deploy.sh
```

The script will:
1. Install any missing tools to `.tools/` (no sudo required)
2. Validate AWS credentials and confirm the target account
3. Deploy infrastructure in two layers (data, then platform)
4. Build container images via CodeBuild
5. Deploy the application via Helm
6. Print the app URL and instructions for creating users

## User Management

After deployment, `deploy.sh` prompts you to create an initial admin user with a temporary password shown in the terminal.

```bash
./manage-users.sh create user@example.com   # Create user, show temp password
./manage-users.sh list                       # List all users
./manage-users.sh delete user@example.com    # Delete a user
```

Users must change the temporary password on first login.

## Updating

After code changes, re-run the deploy script:

```bash
./deploy.sh
```

It's fully idempotent — Terraform converges existing infrastructure, CodeBuild rebuilds images, Helm upgrades the release.

## Teardown

```bash
./destroy.sh
```

This destroys the platform layer (EKS, CloudFront, ALB, Cognito, etc.) while **preserving data** (VPC, RDS database, S3 buckets, EBS volume). The script prints exact commands to delete preserved resources if you want a full cleanup.

To destroy the data layer as well (VPC, RDS, S3 buckets):

```bash
AWS_PROFILE=<your-profile> terraform -chdir=infra/data destroy -auto-approve
```

> **Note:** `destroy.sh` sets `AWS_PROFILE` internally, so it does not persist to your shell session. You must set it explicitly when running Terraform commands manually.

## Security

All traffic is served over HTTPS through CloudFront with WAF protection. The application runs inside a private VPC — the load balancer is not exposed to the internet. Authentication is handled via Amazon Cognito. Secrets are managed through AWS Secrets Manager, and database access uses IAM authentication with no static passwords.

## Run Locally

Test it on your machine with only Bedrock API access needed:

```bash
# Using AWS SSO/profile
AWS_PROFILE=my-profile make dev

# Using a Bedrock API key (no IAM credentials needed)
AWS_BEARER_TOKEN_BEDROCK=your-key make dev
```

Other commands: `make stop`, `make logs`, `make logs s=backend`, `make restart s=backend`, `make build`.

Opens at **http://localhost:4001**. See [local/README.md](local/README.md) for details.

## Development

For architecture details, infrastructure reference, OIDC identity provider configuration, Helm chart structure, manual deployment steps, and troubleshooting, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Acknowledgments

This platform is built on [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) by the UK AI Security Institute, an open-source framework for large language model evaluations.

## Legal Disclaimer

Sample code, software libraries, command line tools, proofs of concept, templates, or other related technology are provided as AWS Content or Third-Party Content under the AWS Customer Agreement, or the relevant written agreement between you and AWS (whichever applies). You should not use this AWS Content or Third-Party Content in your production accounts, or on production or other critical data. You are responsible for testing, securing, and optimizing the AWS Content or Third-Party Content, such as sample code, as appropriate for production grade use based on your specific quality control practices and standards. Deploying AWS Content or Third-Party Content may incur AWS charges for creating or using AWS chargeable resources, such as running Amazon EC2 instances or using Amazon S3 storage.
