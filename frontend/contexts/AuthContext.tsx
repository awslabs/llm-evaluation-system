"use client";

import { createContext, useContext, useEffect, useState, ReactNode } from "react";

interface User {
  id: string;
  email?: string;
  name?: string;
}

interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  logoutUrl: string;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  isLoading: true,
  logoutUrl: "/oauth2/sign_out?rd=/"
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [logoutUrl, setLogoutUrl] = useState("/oauth2/sign_out?rd=/");

  useEffect(() => {
    fetch("/api/auth/user")
      .then((r) => (r.status === 401 ? { user: null, logoutUrl: "/oauth2/sign_out?rd=/" } : r.json()))
      .then((data) => {
        setUser(data.user);
        if (data.logoutUrl) {
          setLogoutUrl(data.logoutUrl);
        }
      })
      .catch(() => setUser(null))
      .finally(() => setIsLoading(false));
  }, []);

  return (
    <AuthContext.Provider value={{ user, isLoading, logoutUrl }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);

export const login = () => {
  window.location.href = "/";
};
