#!/usr/bin/env python3
"""
Dataset MCP Server - HTTP implementation.

Provides CSV dataset upload, analysis, and conversion to YAML format.
Uses a specialized internal agent for intelligent dataset analysis.
"""

import json
import os

from mcp.server import FastMCP
from backend.core.bedrock_client import BedrockClient

# Import the dataset agent
from backend.mcp_servers.dataset.agent import DatasetAgent

# Import save_dataset handler
from backend.mcp_servers.dataset.tools.save_dataset import handle_save_dataset

# Get configuration
region = os.environ.get("AWS_REGION", "us-west-2")
port = int(os.environ.get("DATASET_MCP_SERVER_PORT", "8005"))
host = os.environ.get("HOST", "127.0.0.1")

# Initialize FastMCP server
mcp = FastMCP("dataset-server", port=port, host=host)

# Initialize Bedrock client (singleton)
bedrock = BedrockClient(region=region)


@mcp.tool()
async def analyze_dataset(
    file_content: str,
    filename: str = "dataset.csv",
    user_id: str = None,
) -> str:
    """
    Analyze a CSV dataset for structure and quality.

    Uses an intelligent agent to:
    - Parse the CSV and detect structure
    - Identify question and answer columns
    - Check for data quality issues
    - Determine if the dataset is ready for use

    Args:
        file_content: The raw CSV file content as a string
        filename: Name of the file (for display purposes)

    Returns:
        JSON analysis report with:
        - valid: Whether dataset is usable
        - column_mapping: Detected columns for question/answer
        - issues: List of problems found
        - summary: Human-readable summary
        - usable_rows: Number of complete rows
    """
    # Create a fresh agent for this analysis
    agent = DatasetAgent(bedrock)

    # Run the analysis
    analysis = await agent.analyze(file_content, filename)

    # Return as JSON
    result = {
        "success": True,
        "filename": filename,
        "analysis": analysis,
    }

    return json.dumps(result, indent=2)


@mcp.tool()
async def save_dataset(
    file_content: str,
    filename: str,
    column_mapping: dict,
    user_id: str = None,
) -> str:
    """
    Save a CSV dataset as YAML in promptfoo format.

    Converts the CSV to the required format with vars.question and vars.golden_answer.
    Saves to the user's datasets directory.

    Args:
        file_content: The raw CSV file content
        filename: Original filename (used for naming the output)
        column_mapping: Dict with 'question' and 'golden_answer' keys mapping to CSV column names
        user_id: User ID for storage isolation (auto-injected)

    Returns:
        JSON with:
        - success: Whether save succeeded
        - path: Path to saved YAML file
        - rows_saved: Number of rows converted
    """
    args = {
        "file_content": file_content,
        "filename": filename,
        "column_mapping": column_mapping,
        "user_id": user_id,
    }

    result = await handle_save_dataset(args)
    return result[0].text


if __name__ == "__main__":
    print(f"Starting Dataset MCP Server on http://{host}:{port}/mcp")
    mcp.run(transport="streamable-http")
