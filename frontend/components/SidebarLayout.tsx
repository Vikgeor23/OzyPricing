"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ApiHealthStatus } from "@/components/ApiHealthStatus";
import { BackgroundActivity } from "@/components/BackgroundActivity";
import { ApiHealthProvider } from "@/contexts/ApiHealthContext";
import { api, clearAuthSession, getAuthEmail, getAuthToken } from "@/lib/api";

function IconPlug() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M9 7V2M15 7V2M6 7h12v4a6 6 0 0 1-12 0V7zM12 17v5" />
    </svg>
  );
}

function IconStore() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M3 9l1.5-5h15L21 9M3 9a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0M5 11.5V21h14v-9.5M9 21v-6h6v6" />
    </svg>
  );
}

function IconScales() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 3v18M8 21h8M12 6h7l-3 7a3.5 3.5 0 0 0 6 0l-3-7M12 6H5l3 7a3.5 3.5 0 0 1-6 0l3-7" />
    </svg>
  );
}

const NAV = [
  { href: "/competitors", label: "Ozypricing", icon: <IconStore /> },
  { href: "/comparison", label: "Compare", icon: <IconScales /> },
  { href: "/integrations", label: "Integrations", icon: <IconPlug /> },
];

function initials(email: string | null): string {
  if (!email) return "PM";
  const local = email.split("@")[0] || email;
  const parts = local.split(/[._-]/).filter(Boolean);
  return (parts.length > 1 ? parts[0][0] + parts[1][0] : local.slice(0, 2)).toUpperCase();
}

export function SidebarLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const isLoginPage = pathname === "/login";
  const [authChecked, setAuthChecked] = useState(false);
  const [userEmail, setUserEmail] = useState<string | null>(null);

  useEffect(() => {
    if (isLoginPage) {
      setAuthChecked(true);
      return;
    }
    const token = getAuthToken();
    if (!token) {
      router.replace("/login");
      return;
    }
    setUserEmail(getAuthEmail());
    setAuthChecked(true);
  }, [isLoginPage, pathname, router]);

  async function logout() {
    try {
      await api.post("/auth/logout", undefined);
    } catch {
      /* token may already be invalid */
    }
    clearAuthSession();
    router.replace("/login");
  }

  if (isLoginPage) {
    return <ApiHealthProvider>{children}</ApiHealthProvider>;
  }

  if (!authChecked) {
    return null;
  }

  return (
    <ApiHealthProvider>
      <div className="app-shell">
        <header className="topbar">
          <div className="topbar-brand">
            <Image className="brand-logo topbar-logo" src="/logo.png" alt="Ozypricing" width={260} height={110} priority />
          </div>
          <nav className="topbar-nav" aria-label="Primary navigation">
            {NAV.map((item) => {
              const active = pathname === item.href || pathname.startsWith(item.href + "/");
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={active ? "topbar-link topbar-link-active" : "topbar-link"}
                  aria-current={active ? "page" : undefined}
                >
                  {item.icon}
                  {item.label}
                </Link>
              );
            })}
          </nav>
          <div className="topbar-actions">
            <ApiHealthStatus />
            <div className="topbar-account">
              <span className="topbar-avatar" aria-hidden>
                {initials(userEmail)}
              </span>
              <span className="topbar-user" title={userEmail ?? "Signed in"}>
                {userEmail ?? "Signed in"}
              </span>
            </div>
            <button type="button" className="topbar-logout" onClick={() => void logout()}>
              Log out
            </button>
          </div>
        </header>
        <main className="main-area">{children}</main>
        <BackgroundActivity />
      </div>
    </ApiHealthProvider>
  );
}
