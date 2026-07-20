import type { Metadata } from 'next';
import './globals.css';
import { QueryProvider } from '@/components/QueryProvider';

export const metadata: Metadata = {
  title: 'Trading Journal | Institutional Dashboard',
  description: 'Execution journal & behavioral analytics dashboard',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="bg-obsidian-bg text-slate-100 antialiased selection:bg-win selection:text-obsidian-bg">
        <QueryProvider>{children}</QueryProvider>
      </body>
    </html>
  );
}
