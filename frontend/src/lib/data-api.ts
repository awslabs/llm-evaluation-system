import type {
  DatasetSummary,
  DatasetDetail,
  DocumentEntry,
  JudgeSummary,
  JudgeDetail,
  DatasetTest,
} from "./data-types";

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${txt}`);
  }
  return (await res.json()) as T;
}

export async function listDatasets(search = ""): Promise<DatasetSummary[]> {
  const qs = search ? `?search=${encodeURIComponent(search)}` : "";
  const data = await jsonOrThrow<{ datasets: DatasetSummary[] }>(
    await fetch(`/api/datasets${qs}`),
  );
  return data.datasets ?? [];
}

export async function getDataset(
  id: string,
  offset = 0,
  limit = 50,
  owner?: string | null,
): Promise<DatasetDetail> {
  const ownerQs = owner ? `&owner=${encodeURIComponent(owner)}` : "";
  return jsonOrThrow<DatasetDetail>(
    await fetch(`/api/datasets/${encodeURIComponent(id)}?offset=${offset}&limit=${limit}${ownerQs}`),
  );
}

export async function patchDataset(
  id: string,
  patch: { name?: string; tests?: DatasetTest[] },
): Promise<{ id: string; name: string; total: number; updated_at?: number }> {
  return jsonOrThrow(
    await fetch(`/api/datasets/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function deleteDataset(id: string): Promise<void> {
  const res = await fetch(`/api/datasets/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
}

export function exportDatasetUrl(id: string): string {
  return `/api/datasets/${encodeURIComponent(id)}/export`;
}

export async function listDocuments(): Promise<{ documents: DocumentEntry[]; storage: string }> {
  return jsonOrThrow(await fetch("/api/documents/list"));
}

export async function listJudges(search = ""): Promise<JudgeSummary[]> {
  const qs = search ? `?search=${encodeURIComponent(search)}` : "";
  const data = await jsonOrThrow<{ judges: JudgeSummary[] }>(
    await fetch(`/api/judges${qs}`),
  );
  return data.judges ?? [];
}

export async function getJudge(id: string, owner?: string | null): Promise<JudgeDetail> {
  const ownerQs = owner ? `?owner=${encodeURIComponent(owner)}` : "";
  return jsonOrThrow<JudgeDetail>(
    await fetch(`/api/judges/${encodeURIComponent(id)}${ownerQs}`),
  );
}

export async function deleteJudge(id: string): Promise<void> {
  const res = await fetch(`/api/judges/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
}
