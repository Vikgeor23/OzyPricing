import type { Metadata } from "next";
import "./globals.css";
import { SidebarLayout } from "@/components/SidebarLayout";

export const metadata: Metadata = {
  title: "Ozypricing",
  description: "Competitor price monitoring and matching",
  icons: {
    icon: [{ url: "/favicon.svg", type: "image/svg+xml" }],
    shortcut: "/favicon.svg",
    apple: "/favicon.svg",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <SidebarLayout>{children}</SidebarLayout>
      </body>
    </html>
  );
}
