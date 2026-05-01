"""Test agent for validating sandbox_agent_bridge evaluation flow.

Uses OpenAI SDK format to call model="inspect" via the proxy.
Inspect routes the request to the actual model (bedrock) using
the platform's AWS credentials.
"""

import os
import sys

from openai import OpenAI


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Hello"

    client = OpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:13131/v1"),
        api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
    )

    response = client.chat.completions.create(
        model="inspect",
        messages=[
            {"role": "system", "content": "You are a helpful assistant. Answer concisely."},
            {"role": "user", "content": prompt},
        ],
    )

    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
