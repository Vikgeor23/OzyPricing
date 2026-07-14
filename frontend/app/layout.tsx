import type { Metadata } from "next";
import "./globals.css";
import { SidebarLayout } from "@/components/SidebarLayout";

export const metadata: Metadata = {
  title: "Ozypricing",
  description: "Competitor price monitoring and matching",
  icons: {
    icon: [
      { url: "/favicon.svg", type: "image/svg+xml" },
      { url: "/favicon.png", type: "image/png" },
      { url: "/icon.png", type: "image/png" },
    ],
    shortcut: "/favicon.svg",
    apple: "/favicon.png",
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
