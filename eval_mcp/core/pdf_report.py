"""PDF report generation for evaluation results.

Hybrid approach: LLM-generated narrative sections (neutral analysis) combined
with programmatic data sections (tables, costs, code snippets). The LLM adds
context from the chat transcript but never touches the numbers.
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

from fpdf import FPDF

from eval_mcp.core.bedrock_client import BedrockClient
from eval_mcp.core.pricing import calculate_cost

logger = logging.getLogger(__name__)


# ============== Report Prompt Template ==============

REPORT_SYSTEM_PROMPT = """\
You are a technical evaluation analyst writing an objective report for a \
decision-maker. Your role is to contextualize evaluation data, not to sell.

RULES:
- State facts and measurements. Do not advocate for any provider or model.
- If results are close (within 5%), say so explicitly — the difference may not \
be statistically significant given the sample size.
- Frame cost differences as "potential savings" not "waste" or "overspending."
- Reference the customer's stated use case from the transcript when available.
- Never use superlatives (best, superior, clearly, obviously). Use comparative \
language with numbers attached.
- Acknowledge tradeoffs — a cheaper model with lower accuracy may still be the \
right choice depending on the use case requirements.
- Do not speculate about future performance or make guarantees.
- If the evaluation has limitations (small sample size, narrow criteria), note them.
- Write in third person. Do not address the reader as "you."

OUTPUT FORMAT:
Return a JSON object with exactly these keys:
{
  "context": "1-2 sentences describing what was evaluated and why (from transcript)",
  "findings": ["finding 1", "finding 2", "finding 3"],
  "tradeoff_analysis": "2-3 sentences on cost vs quality vs latency tradeoffs for this specific use case",
  "considerations": "1-2 sentences on limitations, gaps, or what wasn't tested"
}

Keep each finding to one sentence with a specific number. Total output under 300 words."""


REPORT_USER_PROMPT_TEMPLATE = """\
Write the narrative sections for an evaluation report.

EVALUATION TYPE: {eval_type}

EVALUATION DATA:
- Models compared: {models}
- Evaluation criteria: {criteria}
- Criteria descriptions: {criteria_descriptions}
- Sample size: {sample_count} test cases
- Overall scores: {scores_summary}
- Per-criterion breakdown: {per_criterion_summary}
- Cost per 1000 calls: {cost_summary}
- Latency: {latency_summary}
- Token usage: {token_summary}

SAMPLE-LEVEL PATTERNS:
{sample_patterns}

{transcript_section}

GOLDEN EXAMPLE (for format reference only — do not copy content):
{{
  "context": "This evaluation compared three language models for a medical \
triage chatbot handling 50,000 monthly patient inquiries across symptom \
assessment and urgency classification tasks.",
  "findings": [
    "Model A scored 91% on medical accuracy criteria compared to Model B at \
84% and Model C at 79%, based on 40 test cases evaluated by a panel of 3 \
judges.",
    "Model C delivered responses in 1.2s average latency versus 3.8s for \
Model A, a difference that may matter for real-time triage workflows.",
    "At projected volume of 50,000 monthly calls, Model B costs $142/month \
compared to $89/month for Model C — a 37% difference with a 5-point accuracy \
gap."
  ],
  "tradeoff_analysis": "The accuracy gap between Model A and Model C (12 \
percentage points) is substantial for medical use cases where misclassification \
carries patient safety risk. However, Model A's 3x higher latency and 2.4x \
cost premium may not be justified if the deployment handles non-urgent \
inquiries only.",
  "considerations": "This evaluation used 40 test cases focused on common \
conditions. Performance on rare conditions, multi-turn conversations, and \
non-English inputs was not measured. The 7-point gap between Models A and B \
may not be statistically significant at this sample size."
}}

Now write the narrative sections for this evaluation. Return valid JSON only."""


# ============== PDF Builder ==============


class EvalReportPDF(FPDF):
    """Custom PDF class for evaluation reports."""

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=20)

    @staticmethod
    def _sanitize(text: str) -> str:
        """Replace unicode characters unsupported by Helvetica with ASCII equivalents."""
        replacements = {
            "•": "-",  # bullet
            "–": "-",  # en-dash
            "—": "--",  # em-dash
            "‘": "'",  # left single quote
            "’": "'",  # right single quote
            "“": '"',  # left double quote
            "”": '"',  # right double quote
            "…": "...",  # ellipsis
            " ": " ",  # non-breaking space
        }
        for char, replacement in replacements.items():
            text = text.replace(char, replacement)
        return text.encode("latin-1", errors="replace").decode("latin-1")

    def header(self):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(100, 100, 100)
        self.cell(0, 8, "LLM Evaluation Report", align="L")
        self.cell(0, 8, datetime.now().strftime("%Y-%m-%d"), align="R", new_x="LMARGIN", new_y="NEXT")
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    def section_title(self, title: str):
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(30, 30, 30)
        self.ln(4)
        self.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def subsection_title(self, title: str):
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(50, 50, 50)
        self.ln(2)
        self.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def body_text(self, text: str):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 5, self._sanitize(text))
        self.ln(2)

    def bullet_point(self, text: str):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(40, 40, 40)
        self.cell(5, 5, "-")
        self.multi_cell(0, 5, self._sanitize(text))
        self.ln(1)

    def code_block(self, code: str):
        self.set_font("Courier", "", 9)
        self.set_fill_color(245, 245, 245)
        self.set_text_color(30, 30, 30)
        self.ln(2)
        for line in code.split("\n"):
            self.cell(0, 5, f"  {self._sanitize(line)}", new_x="LMARGIN", new_y="NEXT", fill=True)
        self.ln(3)

    def score_table(self, models: list[str], criteria: list[str], aggregate: dict):
        """Render a comparison table with scores per model per criterion."""
        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(240, 240, 240)
        self.set_text_color(30, 30, 30)

        # Calculate column widths
        criteria_col_w = 50
        n_models = len(models)
        model_col_w = min(40, (190 - criteria_col_w) / max(n_models, 1))

        # Header row
        self.cell(criteria_col_w, 7, "Criterion", border=1, fill=True)
        for model in models:
            display_name = model.split("/")[-1] if "/" in model else model
            display_name = display_name[:18]
            self.cell(model_col_w, 7, display_name, border=1, fill=True, align="C")
        self.ln()

        # Data rows
        self.set_font("Helvetica", "", 9)
        for criterion in criteria:
            self.cell(criteria_col_w, 6, criterion[:25], border=1)
            for model in models:
                model_data = aggregate.get(model, {})
                by_criterion = model_data.get("byCriterion", {})
                score = by_criterion.get(criterion, 0)
                score_str = f"{score * 100:.0f}%" if isinstance(score, float) else str(score)
                self.cell(model_col_w, 6, score_str, border=1, align="C")
            self.ln()

        # Overall row
        self.set_font("Helvetica", "B", 9)
        self.cell(criteria_col_w, 7, "OVERALL", border=1, fill=True)
        for model in models:
            model_data = aggregate.get(model, {})
            overall = model_data.get("overall", 0)
            score_str = f"{overall * 100:.0f}%"
            self.cell(model_col_w, 7, score_str, border=1, fill=True, align="C")
        self.ln(4)

    def cost_table(self, models: list[str], stats: dict, monthly_volume: int = 10000):
        """Render cost comparison table with projections."""
        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(240, 240, 240)

        col_widths = [50, 35, 35, 35, 35]
        headers = ["Model", "Cost/1K calls", "Monthly", "Annual", "Avg Latency"]

        for i, h in enumerate(headers):
            self.cell(col_widths[i], 7, h, border=1, fill=True, align="C")
        self.ln()

        self.set_font("Helvetica", "", 9)
        for model in models:
            display_name = model.split("/")[-1] if "/" in model else model
            display_name = display_name[:22]
            model_stats = stats.get(model, {})

            cost = model_stats.get("cost", 0)
            cost_per_1k = cost * 1000 / max(model_stats.get("sample_count", 1), 1)
            monthly_cost = cost_per_1k * (monthly_volume / 1000)
            annual_cost = monthly_cost * 12

            latency = model_stats.get("latencySeconds", 0)
            latency_str = f"{latency:.1f}s" if latency else "N/A"

            self.cell(col_widths[0], 6, display_name, border=1)
            self.cell(col_widths[1], 6, f"${cost_per_1k:.2f}", border=1, align="C")
            self.cell(col_widths[2], 6, f"${monthly_cost:.0f}", border=1, align="C")
            self.cell(col_widths[3], 6, f"${annual_cost:.0f}", border=1, align="C")
            self.cell(col_widths[4], 6, latency_str, border=1, align="C")
            self.ln()
        self.ln(4)


# ============== Report Generation ==============


def _build_scores_summary(aggregate: dict) -> str:
    """Build a concise scores summary string for the LLM prompt."""
    parts = []
    for model, data in aggregate.items():
        name = model.split("/")[-1] if "/" in model else model
        overall = data.get("overall", 0)
        parts.append(f"{name}: {overall * 100:.0f}%")
    return ", ".join(parts)


def _build_cost_summary(stats: dict) -> str:
    """Build a concise cost summary string for the LLM prompt."""
    parts = []
    for model, data in stats.items():
        name = model.split("/")[-1] if "/" in model else model
        cost = data.get("cost", 0)
        sample_count = max(data.get("sample_count", 1), 1)
        cost_per_1k = cost * 1000 / sample_count
        parts.append(f"{name}: ${cost_per_1k:.3f}")
    return ", ".join(parts) if parts else "Not available"


def _build_latency_summary(stats: dict) -> str:
    """Build latency summary string."""
    parts = []
    for model, data in stats.items():
        name = model.split("/")[-1] if "/" in model else model
        latency = data.get("latencySeconds", 0)
        if latency:
            parts.append(f"{name}: {latency:.1f}s avg")
    return ", ".join(parts) if parts else "Not available"


def _detect_eval_type(detail: dict) -> str:
    """Detect evaluation type from detail data."""
    if detail.get("pipeline"):
        return "Agent Analysis"
    if detail.get("promptComparison") or detail.get("prompts"):
        return "Prompt Comparison"
    return "Model Comparison"


def _build_sample_patterns(samples: list[dict], models: list[str]) -> str:
    """Summarize sample-level results: where models agreed/diverged, failure cases."""
    if not samples or not models:
        return "No sample-level data available."

    total = len(samples)
    all_passed = 0
    all_failed = 0
    diverged = []

    for sample in samples:
        results = sample.get("results", {})
        passes = {m: results.get(m, {}).get("passed", False) for m in models if m in results}
        if not passes:
            continue
        if all(passes.values()):
            all_passed += 1
        elif not any(passes.values()):
            all_failed += 1
        else:
            diverged.append({
                "input": sample.get("input", "")[:100],
                "results": {m: "pass" if p else "fail" for m, p in passes.items()},
            })

    lines = [
        f"- All models passed: {all_passed}/{total} samples",
        f"- All models failed: {all_failed}/{total} samples",
        f"- Models disagreed: {len(diverged)}/{total} samples",
    ]

    if diverged:
        lines.append("- Disagreement examples (first 5):")
        for d in diverged[:5]:
            model_results = ", ".join(
                f"{m.split('/')[-1]}={r}" for m, r in d["results"].items()
            )
            lines.append(f"  Input: \"{d['input']}...\" -> {model_results}")

    return "\n".join(lines)


def _build_code_snippet(recommended_model: str) -> str:
    """Build a code snippet showing how to switch to the recommended model."""
    if "bedrock" in recommended_model.lower() or "anthropic" in recommended_model.lower():
        return f'''import boto3
client = boto3.client("bedrock-runtime", region_name="us-west-2")
response = client.invoke_model(modelId="{recommended_model}")'''
    elif "openai" in recommended_model.lower() or "gpt" in recommended_model.lower():
        model_name = recommended_model.split("/")[-1]
        return f'''from openai import OpenAI
client = OpenAI()
response = client.chat.completions.create(model="{model_name}")'''
    elif "google" in recommended_model.lower() or "gemini" in recommended_model.lower():
        model_name = recommended_model.split("/")[-1]
        return f'''from google import genai
client = genai.Client()
response = client.models.generate_content(model="{model_name}")'''
    else:
        model_name = recommended_model.split("/")[-1]
        return f'''# Provider: {recommended_model.split("/")[0] if "/" in recommended_model else "unknown"}
model_id = "{model_name}"
# See provider documentation for integration code'''


async def generate_narrative(
    bedrock: BedrockClient,
    detail: dict,
    transcript: Optional[list[dict]] = None,
) -> dict:
    """Call Claude to generate the narrative sections of the report.

    Returns dict with: context, findings, tradeoff_analysis, considerations.
    Falls back to generic text if the LLM call fails.
    """
    import asyncio

    eval_type = _detect_eval_type(detail)
    models = detail.get("models", [])
    criteria = detail.get("criteria", [])
    aggregate = detail.get("aggregate", {})
    stats = detail.get("stats", {})
    sample_count = len(detail.get("samples", []))

    # Build transcript section — pass the full conversation so the LLM can
    # understand what problem the customer walked in with, their volume,
    # constraints, and requirements mentioned at any point in the session.
    transcript_section = ""
    if transcript:
        relevant_messages = [
            m for m in transcript
            if m.get("role") in ("user", "assistant")
        ]
        if relevant_messages:
            transcript_text = "\n".join(
                f"[{m['role']}]: {m['content']}"
                for m in relevant_messages
            )
            transcript_section = (
                "FULL CONVERSATION TRANSCRIPT (contains the customer's use case, "
                "requirements, volume, constraints, and context):\n"
                f"{transcript_text}"
            )

    # Build per-criterion breakdown
    per_criterion_parts = []
    for model, data in aggregate.items():
        name = model.split("/")[-1] if "/" in model else model
        by_crit = data.get("byCriterion", {})
        if by_crit:
            crit_scores = ", ".join(f"{c}: {v * 100:.0f}%" for c, v in by_crit.items())
            per_criterion_parts.append(f"{name} — {crit_scores}")
    per_criterion_summary = "\n  ".join(per_criterion_parts) if per_criterion_parts else "N/A"

    # Build token usage summary
    token_parts = []
    for model, data in stats.items():
        name = model.split("/")[-1] if "/" in model else model
        tokens = data.get("total_tokens", 0)
        if tokens:
            token_parts.append(f"{name}: {tokens:,} total tokens")
    token_summary = ", ".join(token_parts) if token_parts else "Not available"

    # Build sample-level patterns — show where models diverged (failures, disagreements)
    samples = detail.get("samples", [])
    sample_patterns = _build_sample_patterns(samples, models)

    # Criteria descriptions (what the judge was evaluating)
    criteria_descriptions = detail.get("criteriaDescriptions", {})
    criteria_desc_str = "; ".join(
        f"{k}: {v}" for k, v in criteria_descriptions.items()
    ) if criteria_descriptions else "Not specified"

    prompt = REPORT_USER_PROMPT_TEMPLATE.format(
        eval_type=eval_type,
        models=", ".join(models),
        criteria=", ".join(criteria) if criteria else "overall quality",
        criteria_descriptions=criteria_desc_str,
        sample_count=sample_count,
        scores_summary=_build_scores_summary(aggregate),
        per_criterion_summary=per_criterion_summary,
        cost_summary=_build_cost_summary(stats),
        latency_summary=_build_latency_summary(stats),
        token_summary=token_summary,
        sample_patterns=sample_patterns,
        transcript_section=transcript_section,
    )

    try:
        response = await asyncio.to_thread(
            bedrock.create_message,
            messages=[{"role": "user", "content": prompt}],
            system=REPORT_SYSTEM_PROMPT,
            max_tokens=1024,
            temperature=0.3,
        )
        text = bedrock.extract_text_from_response(response)

        # Parse JSON from response (handle markdown code blocks)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        narrative = json.loads(text)
        return narrative

    except Exception as e:
        logger.warning(f"LLM narrative generation failed: {e}")
        return {
            "context": f"This evaluation compared {len(models)} models across {sample_count} test cases.",
            "findings": [
                _build_scores_summary(aggregate),
                _build_cost_summary(stats),
                _build_latency_summary(stats),
            ],
            "tradeoff_analysis": "See the data tables below for detailed comparison.",
            "considerations": "Review sample-level results for edge cases and failure modes.",
        }


def _get_recommended_model(aggregate: dict) -> str:
    """Pick the highest-scoring model (ties broken by name for determinism)."""
    if not aggregate:
        return "N/A"
    ranked = sorted(
        aggregate.items(),
        key=lambda x: (-x[1].get("overall", 0), x[0]),
    )
    return ranked[0][0] if ranked else "N/A"


async def generate_pdf_report(
    detail: dict,
    bedrock: BedrockClient,
    transcript: Optional[list[dict]] = None,
    monthly_volume: int = 10000,
) -> bytes:
    """Generate a complete PDF report for an evaluation.

    Args:
        detail: Pre-computed evaluation detail JSON.
        bedrock: Bedrock client for LLM narrative generation.
        transcript: Optional chat session messages for context.
        monthly_volume: Projected monthly call volume for cost projections.

    Returns:
        PDF content as bytes.
    """
    models = detail.get("models", [])
    criteria = detail.get("criteria", [])
    aggregate = detail.get("aggregate", {})
    stats = detail.get("stats", {})
    samples = detail.get("samples", [])
    eval_type = _detect_eval_type(detail)
    recommended = _get_recommended_model(aggregate)

    # Generate LLM narrative
    narrative = await generate_narrative(bedrock, detail, transcript)

    # Build PDF
    pdf = EvalReportPDF()
    pdf.alias_nb_pages()
    pdf.add_page()

    # Title
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 12, "LLM Evaluation Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, f"Type: {eval_type}  |  Models: {len(models)}  |  "
             f"Test Cases: {len(samples)}  |  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # --- Section 1: Context (LLM) ---
    pdf.section_title("Context")
    pdf.body_text(narrative.get("context", ""))

    # --- Section 2: Key Findings (LLM) ---
    pdf.section_title("Key Findings")
    for finding in narrative.get("findings", []):
        pdf.bullet_point(finding)

    # --- Section 3: Score Comparison (Programmatic) ---
    pdf.section_title("Score Comparison")
    if criteria and aggregate:
        pdf.score_table(models, criteria, aggregate)
    else:
        # Simple overall scores
        for model, data in aggregate.items():
            name = model.split("/")[-1] if "/" in model else model
            overall = data.get("overall", 0)
            pdf.body_text(f"{name}: {overall * 100:.0f}%")

    # Confidence score
    if aggregate:
        avg_score = sum(d.get("overall", 0) for d in aggregate.values()) / len(aggregate)
        pdf.subsection_title("Evaluation Confidence")
        pdf.body_text(
            f"Average model agreement with evaluation criteria: {avg_score * 100:.0f}%. "
            f"Based on {len(samples)} test cases"
            f"{f' across {len(criteria)} criteria' if criteria else ''}."
        )

    # --- Section 4: Cost Analysis (Programmatic) ---
    pdf.section_title("Cost Analysis")
    pdf.body_text(f"Projected monthly volume: {monthly_volume:,} calls")
    if stats:
        pdf.cost_table(models, stats, monthly_volume)

    # Savings calculation
    if len(models) >= 2 and stats:
        costs = {}
        for model in models:
            model_stats = stats.get(model, {})
            cost = model_stats.get("cost", 0)
            sample_count = max(model_stats.get("sample_count", 1), 1)
            costs[model] = cost * 1000 / sample_count
        if costs:
            cheapest = min(costs, key=costs.get)
            most_expensive = max(costs, key=costs.get)
            if costs[most_expensive] > 0:
                savings_pct = (1 - costs[cheapest] / costs[most_expensive]) * 100
                monthly_savings = (costs[most_expensive] - costs[cheapest]) * (monthly_volume / 1000)
                pdf.body_text(
                    f"Potential savings: switching from {most_expensive.split('/')[-1]} to "
                    f"{cheapest.split('/')[-1]} saves ~${monthly_savings:.0f}/month "
                    f"({savings_pct:.0f}% reduction)."
                )

    # --- Section 5: Tradeoff Analysis (LLM) ---
    pdf.section_title("Tradeoff Analysis")
    pdf.body_text(narrative.get("tradeoff_analysis", ""))

    # --- Section 6: Prompt Comparison specifics ---
    if eval_type == "Prompt Comparison" and detail.get("prompts"):
        pdf.section_title("Prompt Variants Tested")
        for i, prompt_text in enumerate(detail["prompts"], 1):
            pdf.subsection_title(f"Variant {i}")
            pdf.body_text(prompt_text[:500])

    # --- Section 7: Agent Pipeline specifics ---
    if eval_type == "Agent Analysis" and detail.get("pipeline"):
        pdf.section_title("Agent Pipeline Stages")
        for stage in detail["pipeline"]:
            name = stage.get("displayName") or stage.get("name", "")
            scorer = stage.get("scorerType", "")
            stage_criteria = stage.get("criteria", [])
            pdf.subsection_title(f"Stage: {name}")
            pdf.body_text(f"Scorer: {scorer}. Criteria: {', '.join(stage_criteria)}")

    # --- Section 8: Integration (Programmatic) ---
    pdf.section_title("Integration")
    pdf.subsection_title(f"Top-scoring model: {recommended.split('/')[-1] if '/' in recommended else recommended}")
    pdf.code_block(_build_code_snippet(recommended))

    # --- Section 9: Considerations (LLM) ---
    pdf.section_title("Considerations")
    pdf.body_text(narrative.get("considerations", ""))

    # --- Footer note ---
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.body_text(
        "This report presents evaluation data as measured. Model performance varies by "
        "use case, prompt design, and data characteristics. Results should be validated "
        "against production workloads before making infrastructure decisions."
    )

    return bytes(pdf.output())
