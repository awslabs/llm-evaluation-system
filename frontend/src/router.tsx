import { Routes, Route } from "react-router-dom";
import Home from "@/pages/Home";
import Chat from "@/pages/Chat";
import History from "@/pages/History";
import Results from "@/pages/Results";
import Data from "@/pages/Data";
import Optimizations from "@/pages/Optimizations";

// Flat routes — the app has no nested layouts. Query params (?session, ?group,
// ?id) are read inside each page via react-router's useSearchParams, which
// updates the URL and re-renders in dev and prod identically (no static-export
// router caveat, unlike the previous Next.js setup).
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/chat" element={<Chat />} />
      <Route path="/history" element={<History />} />
      <Route path="/results" element={<Results />} />
      <Route path="/data" element={<Data />} />
      <Route path="/optimizations" element={<Optimizations />} />
    </Routes>
  );
}
