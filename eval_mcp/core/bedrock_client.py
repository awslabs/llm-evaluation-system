"""AWS Bedrock client for Claude interactions."""

import configparser
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


_autodetect_lock = threading.Lock()
_autodetect_done = False
_autodetect_error: Optional[Exception] = None

# Where the user's chosen AWS profile is persisted across MCP restarts.
# Routing is by NAME only — token validity is never used to route, so a flipped
# SSO login state can never silently switch the MCP to a different account.
PROFILE_CONFIG_PATH = os.path.expanduser("~/.config/eval-mcp/profile")


def _list_configured_aws_profiles() -> List[str]:
    config_path = os.path.expanduser("~/.aws/config")
    if not os.path.exists(config_path):
        return []
    cp = configparser.ConfigParser()
    try:
        cp.read(config_path)
    except configparser.Error:
        return []
    profiles: List[str] = []
    for section in cp.sections():
        if section.startswith("profile "):
            profiles.append(section.split(" ", 1)[1])
        elif section == "default":
            profiles.append("default")
    return profiles


def _read_saved_profile() -> Optional[str]:
    try:
        with open(PROFILE_CONFIG_PATH) as f:
            name = f.read().strip()
            return name or None
    except OSError:
        return None


def _save_profile(name: str) -> None:
    try:
        os.makedirs(os.path.dirname(PROFILE_CONFIG_PATH), exist_ok=True)
        with open(PROFILE_CONFIG_PATH, "w") as f:
            f.write(name + "\n")
    except OSError as e:
        logger.warning("Could not persist AWS profile choice to %s: %s",
                       PROFILE_CONFIG_PATH, e)


def _is_profile_logged_in(name: str) -> bool:
    """Best-effort check used only to annotate the first-run prompt. Never
    used to route — routing is always by saved name."""
    try:
        boto3.Session(profile_name=name).client("sts").get_caller_identity()
        return True
    except Exception:
        return False


def _autodetect_aws_profile() -> None:
    """Resolve which AWS profile the MCP should use, in priority order:

      1. AWS credentials already in env (AWS_PROFILE / AWS_ACCESS_KEY_ID /
         AWS_BEARER_TOKEN_BEDROCK) → leave them alone.
      2. Saved choice in ~/.config/eval-mcp/profile → use it.
      3. Exactly one profile in ~/.aws/config → use it and persist the name.
      4. Multiple profiles → record an "ambiguous" error message that Bedrock
         tool entry points retrieve via get_autodetect_error() and surface to
         the user. **Never raises from this function**, so importing the MCP
         server (which constructs a BedrockClient at module load) cannot crash
         the whole process before Claude Code can talk to it.

    Token validity is never used to route — only the saved name. If the chosen
    profile's SSO token has expired, boto3 will surface a clear auth error and
    `aws sso login --profile <name>` fixes it. The MCP will not silently
    reroute to a different account.

    Runs at most once per process; result is cached.
    """
    global _autodetect_done, _autodetect_error
    with _autodetect_lock:
        if _autodetect_done:
            return
        _autodetect_done = True

        # (1) caller already chose
        if (
            os.environ.get("AWS_PROFILE")
            or os.environ.get("AWS_ACCESS_KEY_ID")
            or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        ):
            return

        configured = _list_configured_aws_profiles()

        # (2) saved choice
        saved = _read_saved_profile()
        if saved and saved in configured:
            os.environ["AWS_PROFILE"] = saved
            return
        # saved-but-no-longer-configured → fall through and reprompt

        if not configured:
            # No AWS config; let boto3 surface its own credential error.
            return

        # (3) single configured profile — unambiguous, auto-pick and persist
        if len(configured) == 1:
            os.environ["AWS_PROFILE"] = configured[0]
            _save_profile(configured[0])
            logger.info("Auto-selected sole AWS profile: %s", configured[0])
            return

        # (4) ambiguous — record the error for tool entry points to surface.
        # Do NOT raise here: this runs at module import time via
        # BedrockClient.__init__, and raising would kill the MCP before it
        # can serve any tools — Claude Code just sees "Failed to connect."
        lines = []
        for name in configured:
            try:
                logged_in = _is_profile_logged_in(name)
            except Exception:
                # Token probe itself can throw (e.g. SSO token registration
                # expired). Treat as "not logged in" rather than letting the
                # error escape autodetect.
                logged_in = False
            state = "logged in" if logged_in else "not logged in"
            lines.append(f"  - {name} ({state})")
        _autodetect_error = RuntimeError(
            "Multiple AWS profiles configured — the eval MCP needs you to pick one:\n"
            + "\n".join(lines)
            + "\n\nTo save your choice, either:\n"
            + f"  - write the profile name to {PROFILE_CONFIG_PATH}, or\n"
            + "  - set AWS_PROFILE in the eval MCP env block in ~/.claude.json\n"
            + "Then restart the MCP. The chosen profile is routed by name only; "
            + "if its SSO token expires, run `aws sso login --profile <name>` — "
            + "the MCP will not silently switch to a different profile."
        )
        logger.warning("AWS profile autodetect: %s", _autodetect_error)


def get_autodetect_error() -> Optional[Exception]:
    """Return the autodetect ambiguity error, if any. Tool entry points call
    this to convert the error into a structured response instead of letting
    AWS calls fail with cryptic 'no credentials' messages."""
    _autodetect_aws_profile()
    return _autodetect_error


def raise_if_autodetect_error() -> None:
    """Raise the autodetect ambiguity error if one is recorded. Call this at
    the entry to any AWS-invoking code path that doesn't already format the
    error itself, so cryptic boto3 'Unable to locate credentials' failures
    get replaced with the actionable multi-profile message."""
    err = get_autodetect_error()
    if err is not None:
        raise err


def create_boto3_bedrock_client(service: str = "bedrock-runtime", region: str = "us-west-2", **extra_config):
    """Create a boto3 Bedrock client with API key support if configured.

    When AWS_BEARER_TOKEN_BEDROCK is set, creates a client that uses bearer token
    auth instead of SigV4 signing. Otherwise returns a standard boto3 client.
    """
    _autodetect_aws_profile()
    bearer_token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    config_kwargs = {
        "region_name": region,
        "retries": {"max_attempts": 10, "mode": "adaptive"},
        **extra_config,
    }
    if bearer_token:
        config_kwargs["signature_version"] = UNSIGNED

    client = boto3.client(service, config=Config(**config_kwargs))

    if bearer_token:
        def inject_bearer(request, **kwargs):
            request.headers["Authorization"] = f"Bearer {bearer_token}"
        client.meta.events.register("before-send", inject_bearer)

    return client


class BedrockClient:
    """Client for interacting with Claude on AWS Bedrock.

    Singleton pattern: Only one boto3 client instance is created per region.
    Concurrency limiting: Semaphore limits concurrent Bedrock API calls to 100
    to avoid overwhelming AWS Bedrock's infrastructure capacity.
    """

    _instances: Dict[str, "BedrockClient"] = {}
    _semaphore = threading.Semaphore(100)  # Max 100 concurrent Bedrock API calls

    def __new__(cls, region: str = "us-west-2") -> "BedrockClient":
        """Singleton: Return existing instance for this region if it exists."""
        if region not in cls._instances:
            instance = super().__new__(cls)
            cls._instances[region] = instance
            instance._initialized = False
        return cls._instances[region]

    def __init__(self, region: str = "us-west-2") -> None:
        """Initialize Bedrock client (only once per region)."""
        if self._initialized:
            return

        self.region = region
        self.model_id = os.environ.get(
            "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
        )

        self.client = create_boto3_bedrock_client(
            "bedrock-runtime", region,
            max_pool_connections=100,
            read_timeout=300,  # 5 minutes for PDF processing
            connect_timeout=30,
        )
        self._initialized = True

    def convert_mcp_tools_to_claude(self, mcp_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Convert MCP tool format to Claude tool format.

        MCP tools have: name, description, inputSchema
        Claude tools need: name, description, input_schema
        """
        claude_tools = []

        for tool in mcp_tools:
            # Ensure description is non-null string (Bedrock requirement)
            description = tool.get("description")
            if description is None or description == "":
                description = f"Tool: {tool['name']}"

            claude_tool = {
                "name": tool["name"],
                "description": description,
                "input_schema": tool.get("inputSchema", {"type": "object", "properties": {}}),
            }
            claude_tools.append(claude_tool)

        return claude_tools

    def create_message(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Dict[str, Any]] = None,
        system: Optional[str] = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Send a message to Claude and get response.

        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of tools in Claude format
            tool_choice: Optional tool choice to force tool use. Examples:
                - {"type": "auto"} - let model decide (default)
                - {"type": "any"} - force model to use some tool
                - {"type": "tool", "name": "tool_name"} - force specific tool
            system: Optional system prompt
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (0.0 = deterministic, default for reproducibility)

        Returns:
            Response dict from Claude
        """
        raise_if_autodetect_error()
        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }

        if system:
            request_body["system"] = system

        if tools:
            request_body["tools"] = tools

        if tool_choice:
            request_body["tool_choice"] = tool_choice

        # Use semaphore to limit concurrent Bedrock API calls
        with self._semaphore:
            for attempt in range(3):
                try:
                    response = self.client.invoke_model(
                        modelId=self.model_id,
                        body=json.dumps(request_body),
                    )

                    response_body = json.loads(response["body"].read())
                    return response_body

                except ClientError as e:
                    if e.response["Error"]["Code"] == "InvalidSignatureException" and attempt < 2:
                        logger.warning("Clock skew detected, retrying in %ds...", attempt + 1)
                        time.sleep(attempt + 1)
                        continue
                    raise RuntimeError(f"Bedrock API call failed: {e}")
                except Exception as e:
                    raise RuntimeError(f"Bedrock API call failed: {e}")

    async def create_message_streaming(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Dict[str, Any]] = None,
        system: Optional[str] = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ):
        """
        Stream a message response from Claude token-by-token.

        Yields:
            dict: {"type": "text", "text": "..."} for text tokens
                  {"type": "end", "stop_reason": "...", "response": {...}} at end
        """
        raise_if_autodetect_error()
        import asyncio

        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }

        if system:
            request_body["system"] = system

        if tools:
            request_body["tools"] = tools

        if tool_choice:
            request_body["tool_choice"] = tool_choice

        # Run sync boto3 call in thread pool to not block event loop
        loop = asyncio.get_event_loop()

        def _invoke():
            with self._semaphore:
                for attempt in range(3):
                    try:
                        return self.client.invoke_model_with_response_stream(
                            modelId=self.model_id,
                            body=json.dumps(request_body),
                        )
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "InvalidSignatureException" and attempt < 2:
                            logger.warning("Clock skew detected, retrying in %ds...", attempt + 1)
                            time.sleep(attempt + 1)
                            continue
                        raise

        response = await loop.run_in_executor(None, _invoke)

        content_blocks = []
        current_block = None
        stop_reason = None

        # Process stream - yield control after each chunk
        for event in response.get("body"):
            chunk = json.loads(event["chunk"]["bytes"])
            event_type = chunk.get("type")

            if event_type == "content_block_start":
                block = chunk.get("content_block", {})
                if block.get("type") == "text":
                    current_block = {"type": "text", "text": ""}
                elif block.get("type") == "tool_use":
                    current_block = {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": "",
                    }

            elif event_type == "content_block_delta":
                delta = chunk.get("delta", {})
                if delta.get("type") == "text_delta" and current_block:
                    text = delta.get("text", "")
                    current_block["text"] += text
                    yield {"type": "text", "text": text}
                    await asyncio.sleep(0)  # Yield control to event loop
                elif delta.get("type") == "input_json_delta" and current_block:
                    current_block["input"] += delta.get("partial_json", "")

            elif event_type == "content_block_stop":
                if current_block:
                    if current_block["type"] == "tool_use":
                        try:
                            current_block["input"] = json.loads(current_block["input"])
                        except json.JSONDecodeError:
                            current_block["input"] = {}
                    content_blocks.append(current_block)
                    current_block = None

            elif event_type == "message_delta":
                stop_reason = chunk.get("delta", {}).get("stop_reason")

            elif event_type == "message_stop":
                yield {
                    "type": "end",
                    "stop_reason": stop_reason,
                    "response": {"content": content_blocks, "stop_reason": stop_reason},
                }

    def extract_text_from_response(self, response: Dict[str, Any]) -> str:
        """Extract text content from Claude response."""
        content = response.get("content", [])

        text_parts = []
        for block in content:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))

        return "\n".join(text_parts)

    def extract_tool_uses(self, response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract tool use requests from Claude response."""
        content = response.get("content", [])

        tool_uses = []
        for block in content:
            if block.get("type") == "tool_use":
                tool_uses.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input", {}),
                })

        return tool_uses

    def create_tool_result_content(
        self, tool_use_id: str, result: Any, is_error: bool = False
    ) -> Dict[str, Any]:
        """
        Create tool result content for sending back to Claude.

        Args:
            tool_use_id: The ID from the tool_use block
            result: The result from executing the tool
            is_error: Whether this is an error result

        Returns:
            Tool result content block
        """
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps(result) if not isinstance(result, str) else result,
            "is_error": is_error,
        }
