import type { Metadata } from 'next';
import { Noto_Sans_SC, Space_Mono } from 'next/font/google';
import './globals.css';

const sans = Noto_Sans_SC({
  variable: '--font-monitor-sans',
  subsets: ['latin'],
});

const mono = Space_Mono({
  variable: '--font-monitor-mono',
  subsets: ['latin'],
  weight: ['400', '700'],
});

export const metadata: Metadata = {
  metadataBase: new URL(process.env.SITE_ORIGIN || 'http://localhost:3000'),
  title: '交易所新闻监控',
  description: '7 个交易所合约列表 15 秒轮询；新增合约 1 秒盘口核验与三时间戳监控。',
  openGraph: {
    title: '交易所新闻监控',
    description: '合约列表、首次发现、首次有效盘口与公告匹配的云端监控。',
    images: [{ url: '/og.png', width: 1200, height: 630, alt: '交易所新闻监控' }],
  },
  twitter: {
    card: 'summary_large_image',
    title: '交易所新闻监控',
    description: '7 个交易所的合约与公告高频监控。',
    images: ['/og.png'],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body className={`${sans.variable} ${mono.variable}`}>{children}</body>
    </html>
  );
}
