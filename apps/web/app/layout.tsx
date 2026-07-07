import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Trading Algo",
  description: "Monitoring & control for the Kotak Neo F&O algo",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
