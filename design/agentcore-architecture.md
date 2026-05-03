# AgentCore Architecture: Agent Eval Platform

## Overview

The eval platform runs as an AgentCore agent. The user's agent runs as a separate AgentCore session. We use Inspect AI's `sandbox_agent_bridge` with a custom `AgentCoreSandboxEnvironment` — same pattern as local (Docker) and EKS (K8s). Full Inspect integration: .eval logs, transcript, viewer.

## Architecture

```
AgentCore Session A (YOUR PLATFORM):
  - Your image: Python 3.12, Inspect AI, your backend code
  - Receives user messages via POST /invocations
  - Runs inspect eval when user requests evaluation
  - Calls into Session B via InvokeAgentRuntimeCommand

AgentCore Session B (USER'S AGENT):
  - Their image: their deps, their code, untouched
  - Injected: inspect_sandbox_cli binary (static, no deps)
  - Proxy runs on localhost:13131 inside their container
  - Agent calls localhost:13131 for LLM (set via OPENAI_BASE_URL)
  - File RPC: proxy writes requests, Inspect reads/writes via exec
```

## How It Works (step by step)

### 1. Start user's agent session
```python
agentcore_client.create_agent_runtime(
    containerUri="user-ecr-image:latest",
    environmentVariables={"OPENAI_BASE_URL": "http://localhost:13131/v1"},
)
```

### 2. Inject proxy binary
```python
await sandbox.write_file("/opt/inspect/bin/inspect_sandbox_cli", binary_bytes)
await sandbox.exec(["chmod", "+x", "/opt/inspect/bin/inspect_sandbox_cli"])
```

### 3. Start proxy (background)
```python
await sandbox.exec_remote(["inspect_sandbox_cli", "model_proxy"])
# Proxy now listens on localhost:13131 inside the container
```

### 4. Run agent with prompt
```python
await sandbox.exec(
    ["python", "agent.py", "What is 2+2?"],
    env={"OPENAI_BASE_URL": "http://localhost:13131/v1"}
)
```

### 5. File RPC (automatic, handled by Inspect)
```
Agent calls localhost:13131 → proxy writes /var/tmp/.../requests/abc.json
Inspect polls: exec("ls /var/tmp/.../requests/") → finds abc.json
Inspect reads: exec("cat /var/tmp/.../requests/abc.json")
Inspect calls real LLM (Bedrock, OpenAI, whatever)
Inspect writes: write_file("/var/tmp/.../responses/abc.json", response)
Proxy reads response → returns to agent
```

### 6. Cleanup
```python
# Stop session when eval completes
agentcore_client.stop_runtime_session(runtimeSessionId=session_id)
```

## Custom Sandbox Implementation (~100 lines)

```python
@sandboxenv(name="agentcore")
class AgentCoreSandboxEnvironment(SandboxEnvironment):

    async def exec(self, cmd, input=None, cwd=None, env=None, user=None, timeout=None, **kwargs):
        # Build shell command with env vars
        shell_cmd = ""
        if env:
            shell_cmd += " ".join(f"{k}={v}" for k, v in env.items()) + " "
        if cwd:
            shell_cmd += f"cd {cwd} && "
        shell_cmd += " ".join(cmd)
        
        response = self._client.invoke_agent_runtime_command(
            agentRuntimeArn=self._arn,
            qualifier=self._endpoint,
            runtimeSessionId=self._session_id,
            command={"command": ["/bin/bash", "-c", shell_cmd]},
        )
        # Parse streaming response → ExecResult
        ...

    async def write_file(self, file, contents):
        if isinstance(contents, bytes):
            # Base64 encode for binary
            import base64
            encoded = base64.b64encode(contents).decode()
            await self.exec(["bash", "-c", f"echo '{encoded}' | base64 -d > {file}"])
        else:
            # Text file — use heredoc
            await self.exec(["bash", "-c", f"cat > {file} << 'INSPECTEOF'\n{contents}\nINSPECTEOF"])

    async def read_file(self, file, text=True):
        if text:
            result = await self.exec(["cat", file])
            return result.stdout
        else:
            result = await self.exec(["bash", "-c", f"base64 {file}"])
            import base64
            return base64.b64decode(result.stdout)

    @classmethod
    async def sample_init(cls, task_name, config, metadata):
        # Create AgentCore session for user's image
        ...
        return {"default": env}

    @classmethod
    async def sample_cleanup(cls, task_name, config, environments, interrupted):
        # Stop AgentCore session
        ...
```

## Comparison Across Environments

| | Local | EKS | AgentCore |
|---|---|---|---|
| Sandbox | `DockerSandboxEnvironment` (built-in) | `K8sSandboxEnvironment` (pip install inspect-k8s-sandbox) | `AgentCoreSandboxEnvironment` (custom, ~100 lines) |
| exec() | `docker exec` (~5ms) | `kubectl exec` (~5ms) | `InvokeAgentRuntimeCommand` (~150ms) |
| write_file() | `docker cp` | `kubectl cp` | exec with heredoc/base64 |
| Start container | `docker run` | K8s Job | `create_agent_runtime` session |
| Stop container | `docker rm` | `delete job` | `stop_runtime_session` |
| Polling overhead | ~0ms per LLM call | ~0ms per LLM call | ~450ms per LLM call |
| sandbox_agent_bridge | Yes | Yes | Yes |
| .eval logs | Yes | Yes | Yes |
| Transcript/viewer | Yes | Yes | Yes |
| Dependency isolation | Full (separate container) | Full (separate pod) | Full (separate microVM) |

## Latency Analysis for AgentCore

Per agent LLM call:
- Poll for request: `exec("ls requests/")` → ~150ms
- Read request: `exec("cat request.json")` → ~150ms  
- Call real LLM: 2-5 seconds
- Write response: `exec("cat > response.json")` → ~150ms
- **Overhead: ~450ms on top of 2-5 second LLM call (~10-20% slower)**

For 15 samples × 3 LLM calls each = 45 calls × 450ms = ~20 seconds extra on a 3-5 minute eval.

Acceptable.

## What Gets Injected Into User's Container

1. **inspect_sandbox_cli** — single static binary (~10MB, Go/Rust, no deps)
2. **Python client script** — small generated .py file for file RPC bookkeeping

That's it. No packages installed. No system modifications. Binary only listens on localhost:13131. Cannot access internet, cannot read agent code, cannot escalate privileges.

## Migration Path

1. **Now:** Local with Docker sandbox (already working)
2. **Next:** EKS with inspect-k8s-sandbox (pip install, test)
3. **Future:** AgentCore with custom sandbox (~100 lines)

Same `sandbox_agent_bridge`, same scorers, same .eval logs at every step.
