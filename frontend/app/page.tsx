"use client";

import { useAuth } from "@/contexts/AuthContext";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function Home() {
  const { user, isLoading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && user) {
      router.push("/chat");
    }
  }, [user, isLoading, router]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-white">
        <div className="text-gray-400">Loading...</div>
      </div>
    );
  }

  if (user) {
    return (
      <div className="flex h-screen items-center justify-center bg-white">
        <div className="text-gray-400">Redirecting...</div>
      </div>
    );
  }

  return (
    <div className="flex h-screen items-center justify-center bg-white">
      <button
        onClick={() => window.location.href = "/oauth2/start"}
        className="px-6 py-3 bg-blue-600 text-white font-medium rounded-lg hover:bg-blue-700 transition-colors"
      >
        Sign In
      </button>
    </div>
  );
}
