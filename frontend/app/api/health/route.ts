import { NextResponse } from "next/server";

// Allow this route to be statically exported (BUILD_MODE=export for the
// standalone viewer bundle). In standalone server mode it still responds
// dynamically to the EKS liveness / readiness probes.
export const dynamic = "force-static";

export async function GET() {
  return NextResponse.json({ status: "ok" });
}
