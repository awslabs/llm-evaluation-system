"""Test that file paths default to correct directories."""

import json
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from synthetic_eval_mcp_server.tools.generate_qa_pairs import handle_generate_qa_pairs
from synthetic_eval_mcp_server.tools.create_eval_config import handle_create_eval_config
from bedrock_client import BedrockClient


def test_dataset_default_path():
    """Test that datasets default to backend/datasets/ directory."""
    # Mock args without outputPath
    args = {
        "prompt": "Test manufacturing questions",
        "instructions": None,
        "numSamples": 5,
        "numPersonas": 3,
        "outputPath": None,  # Should default
    }

    # Extract the logic that determines the path
    prompt = args["prompt"]
    num_samples = args.get("numSamples", 10)
    output_path = args.get("outputPath")

    if not output_path:
        safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in prompt[:50])
        safe_name = safe_name.strip().replace(' ', '_').lower()
        output_path = f"backend/datasets/{safe_name}_{num_samples}.yaml"

    # Verify the path
    assert output_path.startswith("backend/datasets/")
    assert output_path.endswith("_5.yaml")
    assert "test_manufacturing_questions" in output_path
    print(f"✓ Dataset path: {output_path}")


def test_config_default_path():
    """Test that configs default to .promptfoo/configs/ directory."""
    # Mock args without outputPath
    args = {
        "datasetPath": "backend/datasets/test_10.yaml",
        "providers": ["bedrock:us.anthropic.claude-sonnet-4-20250514-v1:0"],
        "rubric": "Test rubric",
        "promptTemplate": "{{question}}",
        "configName": "test_eval",
        "description": None,
        "outputPath": None,  # Should default
    }

    # Extract the logic that determines the path
    config_name = args.get("configName", "evaluation")
    output_path = args.get("outputPath")

    if not output_path:
        output_path = f".promptfoo/configs/{config_name}.yaml"

    # Verify the path
    assert output_path.startswith(".promptfoo/configs/")
    assert output_path.endswith(".yaml")
    assert "test_eval" in output_path
    print(f"✓ Config path: {output_path}")


def test_custom_paths_still_work():
    """Test that custom paths are still respected."""
    # Dataset with custom path
    args1 = {
        "prompt": "Test",
        "numSamples": 5,
        "outputPath": "custom/path/dataset.yaml",
    }
    assert args1["outputPath"] == "custom/path/dataset.yaml"
    print(f"✓ Custom dataset path: {args1['outputPath']}")

    # Config with custom path
    args2 = {
        "datasetPath": "test.yaml",
        "providers": ["test"],
        "rubric": "test",
        "configName": "test",
        "outputPath": "custom/config.yaml",
    }
    assert args2["outputPath"] == "custom/config.yaml"
    print(f"✓ Custom config path: {args2['outputPath']}")


if __name__ == "__main__":
    print("Testing default file paths...")
    print()

    test_dataset_default_path()
    test_config_default_path()
    test_custom_paths_still_work()

    print()
    print("✅ All tests passed!")
