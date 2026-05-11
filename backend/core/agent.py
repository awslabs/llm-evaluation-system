"""Agent loop for orchestrating Claude + MCP tools."""

import json
import logging
from typing import Any, Dict, List

from rich.console import Console

from eval_mcp.core.bedrock_client import BedrockClient
from .mcp_client import MultiMCPClient

console = Console()

# Get logger for agent operations
agent_logger = logging.getLogger("mcp_tools.agent")


class Agent:
    """Agent that coordinates between Claude and MCP tools."""

    def __init__(
        self,
        bedrock_client: BedrockClient,
        mcp_client: MultiMCPClient,
        debug: bool = False,
    ) -> None:
        """Initialize agent."""
        self.bedrock = bedrock_client
        self.mcp = mcp_client
        self.debug = debug
        self.conversation_history: List[Dict[str, Any]] = []
        self.tool_descriptions: Dict[str, str] = {}
        self.cancel_info: Dict[str, Any] = {}  # Set by cancel handler with eval info

        # System prompt for Claude
        self.system_prompt = """You are a helpful assistant that helps users evaluate and compare LLM models.

You have access to tools to run evaluations, generate test datasets, and analyze results. You MUST actively USE these tools when users request actions.

IMPORTANT: Use tools silently. Do NOT output tool calls as text, XML, or "<invoke>" tags in your responses.

FILE UPLOAD BEHAVIOR:
When a user uploads files (CSV, PDF, etc.), DO NOT automatically process them or run any tools.
Instead:
1. Acknowledge the upload ("I received your file(s): ...")
2. Briefly describe what you can do with them
3. Ask the user what they would like to do

Only proceed with processing when the user explicitly asks for a specific action.

CORE WORKFLOW - Model Comparison:
When users EXPLICITLY ask to compare models or run evaluations, follow this EXACT sequence:
1. generate_qa_pairs → returns {"dataset": "name", "dataset_id": "..."}
2. generate_judge → pass dataset name from step 1, returns {"judge_id": "...", "name": "..."}
3. create_eval_config → pass dataset name from step 1, judge name from step 2, providers list
4. run_evaluation → pass configName from step 3
5. get_viewer_url → get URL to view results

CRITICAL - ERROR HANDLING:
- If ANY tool returns {"success": false, "error": "..."}, STOP and report the error to the user
- NEVER pretend an evaluation succeeded if run_evaluation failed
- NEVER skip run_evaluation - creating a config is NOT the same as running an evaluation

Available Tools:
- list_documents: List all uploaded documents for the user
  * Call this when user asks about their existing/previous documents
  * Returns paths that can be passed to generate_qa_pairs(documents=[...])
- generate_qa_pairs: Create test dataset with questions and golden answers
  * Persona mode: generate_qa_pairs(prompt="topic description", numSamples=10)
  * Document mode: generate_qa_pairs(documents=["path/to/doc.pdf"], numSamples=10)
  * Large documents are automatically chunked - can generate many QA pairs (20 per chunk)
  * When user uploads documents, use the provided document paths with this tool
  * When user asks to use existing documents, first call list_documents to get paths
  * Returns dataset name (stored in DB for reuse)
- generate_judge: Create evaluation criteria based on QA pairs
  * Pass dataset name from generate_qa_pairs
  * Returns judge name (stored in DB for reuse)
- list_datasets: List all saved datasets by name
- list_judges: List all saved judges by name
- create_eval_config: Create config linking dataset, judge, and providers
  * Pass dataset name (from list_datasets or generate_qa_pairs)
  * Pass judge name (from list_judges or generate_judge)
- run_evaluation: ACTUALLY RUN the evaluation (REQUIRED - don't skip this!)
- list_evaluations: List completed evaluations
- get_evaluation_details: Get detailed results for a specific eval
- list_available_models: Discover all available models (Bedrock + external providers)
- list_bedrock_models: Discover available AWS Bedrock models only
- get_viewer_url: Get URL to view evaluation results
- test_provider: Test if a model is accessible (connectivity check only)

MODEL PROVIDERS:
This system supports multiple model providers for evaluation:

1. AWS Bedrock (always available):
   - Model ID format: "bedrock/us.anthropic.claude-sonnet-4-6"
   - Common models:
     * Claude Sonnet 4.6: bedrock/us.anthropic.claude-sonnet-4-6
     * Claude Opus 4.6: bedrock/us.anthropic.claude-opus-4-6
     * Claude Haiku 4.5: bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0

2. External providers (available when API keys are configured):
   - OpenAI: "openai/gpt-4o", "openai/gpt-4.1", "openai/o3"
   - Anthropic direct API: "anthropic/claude-sonnet-4-6"
   - Google Gemini: "google/gemini-2.5-pro", "google/gemini-2.5-flash"
   - Configure API keys via: make keys (local) or deploy.sh --keys (AWS)

- ALWAYS call list_available_models() to discover which providers and models are available
- Users can compare models ACROSS providers (e.g., Bedrock Claude vs OpenAI GPT-4o)
- Cost and latency are tracked per provider automatically

RULES:
1. USE TOOLS for actions - don't just describe what you would do
2. COMPLETE the full workflow - don't stop after create_eval_config
3. REPORT ERRORS honestly - if run_evaluation fails, tell the user
4. After successful eval, provide viewer URL from get_viewer_url
5. WAIT for explicit user requests before processing uploads - don't be proactive

Example - "compare Claude vs GPT-4o on healthcare":
1. list_available_models() → discover available models
2. generate_qa_pairs(prompt="healthcare questions", numSamples=10) → get dataset="healthcare_10"
3. generate_judge(dataset="healthcare_10", domain="healthcare") → get name="healthcare_criteria"
4. create_eval_config(dataset="healthcare_10", providers=["bedrock/us.anthropic.claude-sonnet-4-6", "openai/gpt-4o"], judge="healthcare_criteria") → get configName
5. run_evaluation(configName=...) → runs eval, returns viewerUrl in response
6. Share the viewerUrl as a markdown link: [View Results](viewerUrl)

Example - user uploads documents:
User: [Uploaded 1 document. Document paths for generate_qa_pairs: ["folder/manual.pdf"]]
Response: "I received your file 'manual.pdf'. I can help you:
- Generate QA pairs from this document for evaluation
- Use it as a knowledge base to compare how different models answer questions about it
What would you like to do?"

Example - user asks to use their existing/previous documents:
User: "Create QA pairs from my uploaded documents" or "Use my existing docs"
1. list_documents() → get list of available document paths
2. generate_qa_pairs(documents=[...paths from step 1...], numSamples=10) → get dataset name
Then continue with generate_judge(dataset=...), create_eval_config(dataset=..., judge=...), etc.
"""

    async def _load_tool_descriptions(self) -> None:
        """Load tool descriptions from MCP resource."""
        try:
            result = await self.mcp.read_resource("eval://docs/tools")

            # Extract text from result
            if hasattr(result, "contents") and result.contents:
                docs_text = result.contents[0].text
                docs_data = json.loads(docs_text)

                # Build description mapping
                for tool in docs_data.get("tools", []):
                    self.tool_descriptions[tool["name"]] = tool["description"]

                if self.debug:
                    console.print(f"[dim]Loaded descriptions for {len(self.tool_descriptions)} tools[/dim]")
        except Exception as e:
            if self.debug:
                console.print(f"[yellow]Warning: Could not load tool descriptions: {e}[/yellow]")
            # Continue without descriptions - use fallback later

    def _fix_orphaned_tool_uses(self) -> None:
        """Fix conversation history if last assistant message has tool_uses without tool_results.

        This can happen if a request was cancelled mid-tool-execution.
        Bedrock requires every tool_use to have a corresponding tool_result.
        """
        if len(self.conversation_history) < 1:
            return

        last_msg = self.conversation_history[-1]

        # Check if last message is from assistant with tool_use blocks
        if last_msg.get("role") != "assistant":
            return

        content = last_msg.get("content", [])
        if not isinstance(content, list):
            return

        # Find tool_use blocks
        tool_uses = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]

        if not tool_uses:
            return

        # Build cancel message with eval info if available
        cancel_message = "[Request was cancelled]"
        eval_id = self.cancel_info.get("evalId")
        if eval_id:
            config_name = self.cancel_info.get("configName", "unknown")
            cancel_message = (
                f"[Evaluation cancelled by user. "
                f"Eval ID: {eval_id}, Config: {config_name}. "
                f"Partial results are saved. To resume, call run_evaluation with resumeEvalId=\"{eval_id}\"]"
            )
            self.cancel_info = {}  # Clear after use

        # Need to add tool_results for each tool_use
        agent_logger.debug(f"Fixing {len(tool_uses)} orphaned tool_use(s) from cancelled request")
        tool_results = []
        for tool_use in tool_uses:
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use.get("id"),
                "content": cancel_message,
            })

        self.conversation_history.append({"role": "user", "content": tool_results})

    async def run_conversation_turn(self, user_message: str) -> str:
        """
        Run one turn of the conversation.

        Args:
            user_message: The user's input

        Returns:
            Claude's response text
        """
        # Fix any orphaned tool_uses from cancelled requests
        self._fix_orphaned_tool_uses()

        # Add user message to history
        self.conversation_history.append({"role": "user", "content": user_message})

        # Load tool descriptions if not already loaded
        if not self.tool_descriptions:
            await self._load_tool_descriptions()

        # Get available MCP tools from all servers
        mcp_tools = await self.mcp.list_tools()

        # Filter out legacy generation tools (we have our own)
        # Also filter out developer debugging tools that confuse the agent workflow
        HIDDEN_TOOLS = ["generate_dataset", "generate_test_cases", "compare_providers", "run_assertion"]
        mcp_tools = [tool for tool in mcp_tools if tool["name"] not in HIDDEN_TOOLS]

        # Enrich tools with descriptions
        for tool in mcp_tools:
            if tool["name"] in self.tool_descriptions:
                tool["description"] = self.tool_descriptions[tool["name"]]
            elif tool["description"] is None:
                # Fallback if description not found
                tool["description"] = f"Tool: {tool['name']}"

        claude_tools = self.bedrock.convert_mcp_tools_to_claude(mcp_tools)

        if self.debug:
            console.print(f"[dim]Available tools: {len(claude_tools)}[/dim]")

        # Run agentic loop
        response_text = await self._agentic_loop(claude_tools)

        return response_text

    async def run_conversation_turn_streaming(self, user_message: str):
        """
        Run one turn of the conversation with streaming progress updates.

        Yields SSE-formatted events during execution.

        Args:
            user_message: The user's input

        Yields:
            dict: Events with 'type' and 'data' keys
        """
        # Fix any orphaned tool_uses from cancelled requests
        self._fix_orphaned_tool_uses()

        # Add user message to history
        self.conversation_history.append({"role": "user", "content": user_message})

        # Load tool descriptions if not already loaded
        if not self.tool_descriptions:
            await self._load_tool_descriptions()

        # Get available MCP tools from all servers
        mcp_tools = await self.mcp.list_tools()

        # Filter out legacy generation tools (we have our own)
        # Also filter out developer debugging tools that confuse the agent workflow
        HIDDEN_TOOLS = ["generate_dataset", "generate_test_cases", "compare_providers", "run_assertion"]
        mcp_tools = [tool for tool in mcp_tools if tool["name"] not in HIDDEN_TOOLS]

        # Enrich tools with descriptions
        for tool in mcp_tools:
            if tool["name"] in self.tool_descriptions:
                tool["description"] = self.tool_descriptions[tool["name"]]
            elif tool["description"] is None:
                tool["description"] = f"Tool: {tool['name']}"

        claude_tools = self.bedrock.convert_mcp_tools_to_claude(mcp_tools)

        yield {"type": "status", "data": {"message": "Thinking..."}}

        # Run agentic loop with streaming
        async for event in self._agentic_loop_streaming(claude_tools):
            yield event

    async def _agentic_loop(self, tools: List[Dict[str, Any]]) -> str:
        """
        Run the agentic loop: Claude thinks, uses tools, thinks again, etc.

        Returns:
            Final text response from Claude
        """
        max_iterations = 20
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            agent_logger.debug(f"Iteration {iteration}/{max_iterations}")
            agent_logger.info(json.dumps({"event": "agent_iteration_start", "iteration": iteration, "max": max_iterations}))

            # Call Claude
            response = self.bedrock.create_message(
                messages=self.conversation_history,
                tools=tools,
                system=self.system_prompt,
            )

            stop_reason = response.get('stop_reason')
            agent_logger.debug(f"Claude response - stop_reason: {stop_reason}")
            agent_logger.info(json.dumps({"event": "claude_response", "iteration": iteration, "stop_reason": stop_reason}))

            if self.debug:
                console.print(f"[dim]Iteration {iteration}, stop_reason: {response.get('stop_reason')}[/dim]")

            # Check stop reason
            stop_reason = response.get("stop_reason")

            if stop_reason == "end_turn":
                # Claude is done, extract text and return
                text = self.bedrock.extract_text_from_response(response)

                # Log final response to user
                agent_logger.info(json.dumps({
                    "event": "agent_final_response",
                    "iteration": iteration,
                    "response": text
                }))

                # Add assistant message to history
                self.conversation_history.append({
                    "role": "assistant",
                    "content": response.get("content", []),
                })

                return text

            elif stop_reason == "tool_use":
                # Claude wants to use tools
                tool_uses = self.bedrock.extract_tool_uses(response)
                text_content = self.bedrock.extract_text_from_response(response)

                # Add assistant message with tool uses to history
                self.conversation_history.append({
                    "role": "assistant",
                    "content": response.get("content", []),
                })

                # Log Claude's reasoning/thinking before tool use
                if text_content:
                    agent_logger.info(json.dumps({
                        "event": "claude_thinking",
                        "iteration": iteration,
                        "thinking": text_content
                    }))
                    if self.debug:
                        console.print(f"[dim]Claude thinking: {text_content}[/dim]")

                # Execute tools and collect results
                tool_results = []
                for tool_use in tool_uses:
                    agent_logger.debug(f"Calling tool: {tool_use['name']} with args: {list(tool_use['input'].keys())}")
                    agent_logger.info(json.dumps({
                        "event": "agent_tool_call",
                        "iteration": iteration,
                        "tool": tool_use['name'],
                        "args_keys": list(tool_use['input'].keys())
                    }))

                    if self.debug:
                        console.print(f"[dim]Calling tool: {tool_use['name']}[/dim]")

                    result = await self._execute_tool(tool_use["name"], tool_use["input"])

                    # Log result summary
                    result_preview = str(result)[:200] if result else "None"
                    agent_logger.debug(f"Tool {tool_use['name']} completed. Result preview: {result_preview}")
                    agent_logger.info(json.dumps({
                        "event": "agent_tool_result",
                        "iteration": iteration,
                        "tool": tool_use['name'],
                        "result_preview": result_preview
                    }))

                    tool_result_content = self.bedrock.create_tool_result_content(
                        tool_use["id"], result
                    )
                    tool_results.append(tool_result_content)

                # Add tool results to history as user message
                self.conversation_history.append({"role": "user", "content": tool_results})

                # Continue loop to get Claude's next response

            else:
                # Unexpected stop reason
                console.print(f"[yellow]Warning: Unexpected stop_reason: {stop_reason}[/yellow]")
                text = self.bedrock.extract_text_from_response(response)
                return text or "I encountered an issue processing your request."

        # Max iterations reached
        agent_logger.warning(f"Agent hit max iterations ({max_iterations})")
        agent_logger.warning(json.dumps({"event": "max_iterations_reached", "max": max_iterations}))

        # Log last few messages to understand what's looping
        last_tools = []
        for msg in self.conversation_history[-6:]:
            if msg["role"] == "assistant":
                assistant_text = self.bedrock.extract_text_from_response({'content': msg['content']})[:100]
                agent_logger.debug(f"  Assistant: {assistant_text}")
                tool_uses = [c for c in msg.get("content", []) if c.get("type") == "tool_use"]
                if tool_uses:
                    tool_names = [t['name'] for t in tool_uses]
                    agent_logger.debug(f"    Tools: {tool_names}")
                    last_tools.extend(tool_names)
            elif msg["role"] == "user" and isinstance(msg.get("content"), list):
                agent_logger.debug(f"  Tool results: {len(msg['content'])} results")

        agent_logger.warning(json.dumps({"event": "max_iterations_context", "last_tools": last_tools}))

        return "I reached the maximum number of thinking steps. Please try rephrasing your request."

    async def _agentic_loop_streaming(self, tools: List[Dict[str, Any]]):
        """
        Run the agentic loop with streaming progress updates.

        Yields events at key points during execution.
        """
        max_iterations = 20
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            agent_logger.debug(f"Iteration {iteration}/{max_iterations}")
            agent_logger.info(json.dumps({"event": "agent_iteration_start", "iteration": iteration, "max": max_iterations}))

            # Call Claude with streaming
            response = None
            stop_reason = None
            streamed_text = ""

            async for event in self.bedrock.create_message_streaming(
                messages=self.conversation_history,
                tools=tools,
                system=self.system_prompt,
            ):
                if event["type"] == "text":
                    streamed_text += event["text"]
                    # Yield text token to frontend
                    yield {"type": "text", "data": {"content": event["text"]}}
                elif event["type"] == "end":
                    stop_reason = event["stop_reason"]
                    response = event["response"]

            agent_logger.debug(f"Claude response - stop_reason: {stop_reason}")
            agent_logger.info(json.dumps({"event": "claude_response", "iteration": iteration, "stop_reason": stop_reason}))

            # Check stop reason
            if stop_reason == "end_turn":
                # Log final response to user
                agent_logger.info(json.dumps({
                    "event": "agent_final_response",
                    "iteration": iteration,
                    "response": streamed_text
                }))

                # Add assistant message to history
                self.conversation_history.append({
                    "role": "assistant",
                    "content": response.get("content", []),
                })

                # Yield completion (text already streamed)
                yield {
                    "type": "complete",
                    "data": {
                        "response": streamed_text,
                        "iterations": iteration
                    }
                }
                return

            elif stop_reason == "tool_use":
                # Claude wants to use tools
                tool_uses = self.bedrock.extract_tool_uses(response)

                # Add assistant message with tool uses to history
                self.conversation_history.append({
                    "role": "assistant",
                    "content": response.get("content", []),
                })

                # Log thinking (already streamed to frontend via text events)
                if streamed_text:
                    agent_logger.info(json.dumps({
                        "event": "claude_thinking",
                        "iteration": iteration,
                        "thinking": streamed_text
                    }))

                # Execute tools and collect results
                tool_results = []
                for tool_use in tool_uses:
                    tool_name = tool_use['name']

                    # Yield tool call event
                    yield {
                        "type": "tool_call",
                        "data": {
                            "tool": tool_name,
                            "args": list(tool_use['input'].keys())
                        }
                    }

                    agent_logger.debug(f"Calling tool: {tool_name} with args: {list(tool_use['input'].keys())}")
                    agent_logger.info(json.dumps({
                        "event": "agent_tool_call",
                        "iteration": iteration,
                        "tool": tool_name,
                        "args_keys": list(tool_use['input'].keys())
                    }))

                    # Execute tool with keep-alive for long-running operations
                    result = None
                    async for event in self._execute_tool_with_keepalive(tool_name, tool_use["input"]):
                        if event["type"] == "result":
                            result = event["data"]
                        else:
                            yield event

                    # Log and yield result
                    result_preview = str(result)[:200] if result else "None"
                    agent_logger.debug(f"Tool {tool_name} completed. Result preview: {result_preview}")
                    agent_logger.info(json.dumps({
                        "event": "agent_tool_result",
                        "iteration": iteration,
                        "tool": tool_name,
                        "result_preview": result_preview
                    }))

                    yield {
                        "type": "tool_result",
                        "data": {
                            "tool": tool_name,
                            "preview": result_preview
                        }
                    }

                    tool_result_content = self.bedrock.create_tool_result_content(
                        tool_use["id"], result
                    )
                    tool_results.append(tool_result_content)

                # Add tool results to history as user message
                self.conversation_history.append({"role": "user", "content": tool_results})

            else:
                # Unexpected stop reason
                text = self.bedrock.extract_text_from_response(response)
                yield {
                    "type": "complete",
                    "data": {
                        "response": text or "I encountered an issue processing your request.",
                        "iterations": iteration,
                        "warning": f"Unexpected stop_reason: {stop_reason}"
                    }
                }
                return

        # Max iterations reached
        yield {
            "type": "error",
            "data": {
                "message": "Reached maximum iterations",
                "iterations": max_iterations
            }
        }

        yield {
            "type": "complete",
            "data": {
                "response": "I reached the maximum number of thinking steps. Please try rephrasing your request.",
                "iterations": max_iterations
            }
        }

    async def _execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Execute an MCP tool and return the result."""
        try:
            result = await self.mcp.call_tool(tool_name, arguments)

            # MCP returns a CallToolResult object, extract the content
            if hasattr(result, "content"):
                # result.content is a list of content blocks
                content_blocks = result.content
                if content_blocks:
                    # For simplicity, concatenate text content
                    text_parts = []
                    for block in content_blocks:
                        if hasattr(block, "text"):
                            text_parts.append(block.text)
                    return "\n".join(text_parts) if text_parts else str(result)

            return str(result)

        except Exception as e:
            error_msg = f"Tool execution failed: {str(e)}"
            if self.debug:
                console.print(f"[red]{error_msg}[/red]")
            return error_msg

    async def _execute_tool_with_keepalive(self, tool_name: str, arguments: Dict[str, Any]):
        """
        Execute tool with periodic keep-alive events for long-running operations.

        Yields progress events every 30 seconds to prevent body timeout.
        Final event has type='result' with the actual result data.
        """
        import asyncio

        tool_task = asyncio.create_task(self._execute_tool(tool_name, arguments))
        elapsed = 0

        while True:
            done, _ = await asyncio.wait({tool_task}, timeout=30)

            if tool_task in done:
                result = tool_task.result()
                yield {"type": "result", "data": result}
                return

            # Send keepalive every 30 seconds
            elapsed += 30
            yield {
                "type": "progress",
                "data": {
                    "message": f"Still working on {tool_name}... ({elapsed}s elapsed)",
                    "elapsed": elapsed
                }
            }

    def clear_history(self) -> None:
        """Clear conversation history."""
        self.conversation_history = []
