"use client";

import Image from "next/image";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, getAuthToken, isApiError, setAuthSession } from "@/lib/api";

type AuthResponse = { token: string; email: string };

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (getAuthToken()) router.replace("/competitors");
  }, [router]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const res = await api.post<AuthResponse>(`/auth/${mode}`, { email, password });
      if (res && "token" in res) {
        setAuthSession(res.token, res.email);
        router.replace("/competitors");
      }
    } catch (err) {
      setError(isApiError(err) ? err.message : "Something went wrong. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <Image className="brand-logo brand-logo-login" src="/logo.png" alt="Ozypricing" width={360} height={240} priority />
        </div>

        <div className="login-tabs">
          <button
            type="button"
            className={mode === "login" ? "login-tab login-tab-active" : "login-tab"}
            onClick={() => {
              setMode("login");
              setError(null);
            }}
          >
            Sign in
          </button>
          <button
            type="button"
            className={mode === "register" ? "login-tab login-tab-active" : "login-tab"}
            onClick={() => {
              setMode("register");
              setError(null);
            }}
          >
            Register
          </button>
        </div>

        <div className="field">
          <label>Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
          />
        </div>
        <div className="field">
          <label>Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            minLength={8}
            required
          />
          {mode === "register" ? (
            <span className="muted" style={{ fontSize: "0.78rem" }}>
              At least 8 characters.
            </span>
          ) : null}
        </div>

        {error ? <p className="section-inline-error" style={{ margin: 0 }}>{error}</p> : null}

        <button className="primary login-submit" type="submit" disabled={busy}>
          {busy ? "One moment…" : mode === "login" ? "Sign in" : "Create account"}
        </button>
      </form>
    </div>
  );
}
