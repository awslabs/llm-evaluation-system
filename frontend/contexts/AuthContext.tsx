"use client";

import { createContext, useContext, useEffect, useState, ReactNode } from "react";

interface User {
  id: string;
  email?: string;
  name?: string;
}

type AppMode = "full" | "viewer";

interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  logoutUrl: string;
  mode: AppMode;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  isLoading: true,
  logoutUrl: "/oauth2/sign_out?rd=/",
  mode: "full",
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [logoutUrl, setLogoutUrl] = useState("/oauth2/sign_out?rd=/");
  const [mode, setMode] = useState<AppMode>("full");

  useEffect(() => {
    fetch("/api/auth/user")
      .then((r) => (r.status === 401 ? { user: null, logoutUrl: "/oauth2/sign_out?rd=/" } : r.json()))
      .then((data) => {
        setUser(data.user);
        if (data.logoutUrl) {
          setLogoutUrl(data.logoutUrl);
        }
        if (data.mode === "viewer") {
          setMode("viewer");
        }
      })
      .catch(() => setUser(null))
      .finally(() => setIsLoading(false));
  }, []);

  return (
    <AuthContext.Provider value={{ user, isLoading, logoutUrl, mode }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);

export const login = () => {
  window.location.href = "/";
};
