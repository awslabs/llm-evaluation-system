export type DatasetSource =
  | { kind: "imported"; origin?: string }
  | { kind: "synthetic"; mode?: string; documents?: string[]; agent?: string; prompt?: string }
  | { kind: "manual" };

export interface DatasetSummary {
  id: string;
  name: string;
  num_samples: number;
  source: DatasetSource;
  created_at: number;
  updated_at?: number;
}

export interface DatasetTest {
  vars?: Record<string, string>;
  [key: string]: unknown;
}

export interface DatasetDetail {
  id: string;
  name: string;
  source: DatasetSource;
  created_at: number;
  updated_at?: number;
  total: number;
  offset: number;
  limit: number;
  tests: DatasetTest[];
}

export interface DocumentEntry {
  path: string;
  size?: number;
  modified?: number;
}

export interface JudgeSummary {
  id: string;
  name: string;
  domain: string;
  criteria: string[];
  created_at: number;
}

export interface JudgeDetail {
  id: string;
  name: string;
  config: {
    domain?: string;
    criteria?: Array<{ name: string; description?: string; weight?: number }>;
    [key: string]: unknown;
  };
  created_at: number;
}

export function sourceLabel(source: DatasetSource | undefined): string {
  if (!source) return "UNKNOWN";
  switch (source.kind) {
    case "imported":
      return source.origin ? `IMPORTED · ${source.origin}` : "IMPORTED";
    case "synthetic": {
      const mode = source.mode || "synthetic";
      if (mode === "agent" && source.agent) return `SYNTHETIC · AGENT`;
      if (mode === "document" && source.documents?.length) {
        return `SYNTHETIC · ${source.documents.length} DOC${source.documents.length === 1 ? "" : "S"}`;
      }
      if (mode === "persona") return "SYNTHETIC · PERSONA";
      return `SYNTHETIC · ${mode.toUpperCase()}`;
    }
    case "manual":
      return "MANUAL";
    default:
      return "UNKNOWN";
  }
}

export function sourceGlyph(source: DatasetSource | undefined): string {
  if (!source) return "·";
  if (source.kind === "synthetic") return "⌬";
  if (source.kind === "imported") return "⤓";
  return "✎";
}

export function formatTimestamp(ms: number | undefined): string {
  if (!ms) return "";
  try {
    return new Date(ms).toLocaleDateString(undefined, {
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}
