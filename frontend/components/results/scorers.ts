// Shared scorer metadata for the results UI. The chip row, the
// per-sample detail panel, and any future scorer-aware surface all
// read these labels + descriptions, so users see the SAME explanation
// of "what does f1 mean" regardless of where they encounter it.

export interface ScorerInfo {
  label: string;
  // Short modifier shown next to the label in the chip row. Keep it
  // small enough that a chip stays scannable on one line.
  short: string;
  // Full one-sentence explanation. Surfaces on hover (chip `title`
  // attribute) and inline in the detail panel.
  description: string;
}

export const SCORER_INFO: Record<string, ScorerInfo> = {
  jury_scorer: {
    label: "Jury",
    short: "multi-judge LLM",
    description:
      "Multiple LLM judges each score every criterion as pass/fail; the sample passes a criterion if the majority of judges pass it. Best for open-ended answers where you've defined criteria.",
  },
  f1: {
    label: "F1",
    short: "token overlap",
    description:
      "Token-level F1 between the model's answer and the reference answer — the harmonic mean of word recall and precision. 1.0 means identical word sets; 0.0 means no words in common. Best for short-answer QA.",
  },
  exact: {
    label: "Exact",
    short: "exact match",
    description:
      "1.0 if the model's answer matches the reference exactly after normalising whitespace, punctuation, and case; 0.0 otherwise. Strict — use for short fixed-form answers.",
  },
  includes: {
    label: "Includes",
    short: "substring",
    description:
      "1.0 if the reference answer appears as a substring anywhere in the model's output (case-insensitive); 0.0 otherwise.",
  },
  match: {
    label: "Match",
    short: "text match",
    description:
      "Configurable string match between the model's answer and the reference. By default the reference must appear at the end of the model's output.",
  },
};

export function scorerInfo(name: string): ScorerInfo {
  return (
    SCORER_INFO[name] || {
      label: name,
      short: "custom",
      description: `Custom scorer: ${name}.`,
    }
  );
}
