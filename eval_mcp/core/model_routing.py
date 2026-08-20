"""Route Claude-on-Bedrock through Inspect's native anthropic provider.

AWS recommends the ``bedrock-runtime`` endpoint, and it serves the native
Anthropic Messages API (the native request body over InvokeModel with
``anthropic_version: bedrock-2023-05-31``) — see
https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html and
inference-messages-api.html. Inspect's ``anthropic/bedrock/<model-id>``
provider string speaks exactly that surface via the official ``anthropic``
SDK, so Claude models get the provider logic Anthropic maintains upstream —
correct thinking gates for every model generation, ``reasoning_effort="none"``
that actually disables thinking, and effort-aware ``max_tokens`` sizing —
instead of the drift-prone re-implementation in Inspect's Converse-based
``bedrock`` provider (which silently dropped ``reasoning_effort`` on Claude 5
and could not disable thinking on 4.7+; both verified live 2026-08-19).

Non-Claude families (Nova, Llama, Mistral, gpt-oss, ...) have no native
Messages API and stay on Converse. GPT-5.x frontier models stay on Bedrock
Mantle (``openai/bedrock/*``) — verified live that bedrock-runtime's
``/openai/v1`` path does not serve them.

The translation is a boundary concern only: user-facing model ids remain
``bedrock/<id>`` everywhere (tool inputs, configs, pricing, stored results,
viewer). ``to_native()`` is applied where model strings are handed to
Inspect; ``from_native()`` normalizes Inspect log names back on ingestion so
everything downstream is unchanged.
"""

_BEDROCK_PREFIX = "bedrock/"
_NATIVE_PREFIX = "anthropic/bedrock/"


def to_native(model_id: str) -> str:
    """Map a Claude ``bedrock/<id>`` to ``anthropic/bedrock/<id>``.

    Anything else — non-Claude Converse models, Mantle models, external
    providers, already-native ids — passes through untouched.
    """
    if model_id.startswith(_BEDROCK_PREFIX) and "anthropic.claude" in model_id:
        return "anthropic/" + model_id
    return model_id


def from_native(model_id: str) -> str:
    """Normalize a native ``anthropic/bedrock/<id>`` log name back to the
    user-facing ``bedrock/<id>`` form."""
    if model_id.startswith(_NATIVE_PREFIX):
        return model_id[len("anthropic/"):]
    return model_id


def supports_thinking_control(model_id: str) -> bool:
    """Whether the ``thinking`` (reasoning-effort) axis actually reaches this
    model through its current route.

    A thinking comparison against a model that silently ignores the setting
    would produce identical runs labeled as different efforts — wrong results
    that look right — so callers must fail loud on unsupported providers
    rather than run. Supported routes:

    - Claude on Bedrock (native anthropic provider — adaptive thinking/effort)
    - gpt-oss and Nova on Converse (Inspect's provider maps reasoning_effort)
    - GPT-5.x on Converse (via the eval_mcp.inspect_patches branch that emits
      the Responses-API ``{"reasoning": {"effort": ...}}`` shape)
    - Mantle ``openai/bedrock/*`` (Responses API carries reasoning effort)
    - First-party external providers (openai/, anthropic/, google/ — their
      native SDK providers handle reasoning config)

    Notably UNSUPPORTED: the Llama/Mistral/DeepSeek/Qwen Converse families —
    those models expose no reasoning-effort control on Bedrock at all.
    """
    mid = model_id.lower()
    if mid.startswith(_BEDROCK_PREFIX):
        return (
            "anthropic.claude" in mid
            or "gpt-oss" in mid
            or "amazon.nova" in mid
            or "openai.gpt-5" in mid
        )
    # Mantle + first-party external providers all ride native SDK providers.
    return mid.startswith(("openai/", "anthropic/", "google/"))
