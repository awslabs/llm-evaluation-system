"""List available evaluation configuration files."""

from pathlib import Path
from typing import Any, Dict, List
from mcp.types import TextContent
import yaml


async def handle_list_eval_configs(args: Dict[str, Any]) -> List[TextContent]:
    """Handle list_eval_configs tool call.

    Lists all evaluation configuration files in the .promptfoo/configs/ directory.
    Returns details about each config including providers, dataset path, and judge.
    """
    try:
        # Get optional search filter
        search_term = (args.get("searchTerm") or "").lower()

        # Look in the standard configs directory
        configs_dir = Path(".promptfoo/configs")

        if not configs_dir.exists():
            return [
                TextContent(
                    type="text",
                    text="No configs directory found. Create your first config with create_eval_config.",
                )
            ]

        # Find all YAML config files
        config_files = list(configs_dir.glob("*.yaml")) + list(configs_dir.glob("*.yml"))

        if not config_files:
            return [
                TextContent(
                    type="text",
                    text="No evaluation configs found in .promptfoo/configs/",
                )
            ]

        # Parse and format config information
        configs_info = []
        for config_file in sorted(config_files):
            try:
                with open(config_file, "r") as f:
                    config_data = yaml.safe_load(f)

                # Extract key information
                config_name = config_file.stem

                # Apply search filter
                if search_term and search_term not in config_name.lower():
                    continue

                providers = config_data.get("providers", [])
                provider_names = []
                for provider in providers:
                    if isinstance(provider, str):
                        provider_names.append(provider)
                    elif isinstance(provider, dict):
                        provider_names.append(provider.get("id", "unknown"))

                tests = config_data.get("tests", [])
                dataset_path = tests[0].get("vars") if tests and isinstance(tests[0].get("vars"), str) else "inline"

                judge = ""
                if tests and "assert" in tests[0]:
                    assertions = tests[0]["assert"]
                    if assertions and isinstance(assertions, list):
                        judge_assert = next((a for a in assertions if a.get("type") == "llm-rubric"), None)
                        if judge_assert:
                            judge = judge_assert.get("value", "")[:100]  # First 100 chars

                config_info = {
                    "name": config_name,
                    "path": str(config_file),
                    "providers": provider_names,
                    "dataset": dataset_path,
                    "judge_preview": judge if judge else "No judge",
                }
                configs_info.append(config_info)

            except Exception:
                # Skip files that can't be parsed
                continue

        if not configs_info:
            return [
                TextContent(
                    type="text",
                    text=f"No configs found matching '{search_term}'" if search_term else "No valid configs found",
                )
            ]

        # Format output
        output = f"Found {len(configs_info)} evaluation config(s):\n\n"

        for config in configs_info:
            output += f"📋 **{config['name']}**\n"
            output += f"   Path: {config['path']}\n"
            output += f"   Providers: {', '.join(config['providers'])}\n"
            output += f"   Dataset: {config['dataset']}\n"
            output += f"   Judge: {config['judge_preview']}\n\n"

        return [TextContent(type="text", text=output)]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=f"Error listing configs: {str(e)}",
            )
        ]
