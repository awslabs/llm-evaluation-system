import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";
import { Providers } from "./providers";

// Self-hosted, OFL-1.1-licensed fonts. See app/fonts/OFL.txt for full license
// text and copyright statements, and /THIRD_PARTY at the repository root for
// the attribution entries.
const instrumentSerif = localFont({
  variable: "--font-instrument-serif",
  display: "swap",
  src: [
    {
      path: "./fonts/InstrumentSerif-Regular.woff2",
      weight: "400",
      style: "normal",
    },
    {
      path: "./fonts/InstrumentSerif-Italic.woff2",
      weight: "400",
      style: "italic",
    },
  ],
});

const geist = localFont({
  variable: "--font-geist",
  display: "swap",
  src: [
    {
      path: "./fonts/Geist-Variable.woff2",
      weight: "100 900",
      style: "normal",
    },
  ],
});

const geistMono = localFont({
  variable: "--font-geist-mono",
  display: "swap",
  src: [
    {
      path: "./fonts/GeistMono-Variable.woff2",
      weight: "100 900",
      style: "normal",
    },
  ],
});

export const metadata: Metadata = {
  title: "LLM Evaluation — Observatory",
  description: "A precision instrument for measuring how language models behave.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${instrumentSerif.variable} ${geist.variable} ${geistMono.variable}`}
    >
      <body className="antialiased font-sans bg-ink text-bone">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
