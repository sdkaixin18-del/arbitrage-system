import { createRoot } from 'react-dom/client';
import { useEffect, useState } from 'react';
import Home from '../app/page';
import '../app/globals.css';
import './style.css';

type ControlStatus = { mode: string; policy?: string; busy: boolean; message: string; replicaAt?: string | null };
async function readControl(response: Response): Promise<ControlStatus> {
  const data = await response.json() as Partial<ControlStatus> & { error?: string };
  if (!response.ok) throw new Error(data.error || '控制服务不可用');
  if (typeof data.mode !== 'string' || typeof data.busy !== 'boolean' || typeof data.message !== 'string') throw new Error('控制服务返回格式异常');
  return data as ControlStatus;
}

function App() {
  const [status, setStatus] = useState<ControlStatus>({ mode: 'paused', busy: false, message: '读取运行状态…' });
  const [error, setError] = useState('');
  const load = () => fetch('/api/control').then(readControl).then(setStatus).catch(() => setError('本地控制服务不可用'));
  useEffect(() => { void load(); const timer = setInterval(() => void load(), 3000); return () => clearInterval(timer); }, []);
  const switchMode = async (mode: string) => {
    setError('');
    try {
      const response = await fetch('/api/control', { method: 'POST', headers: { 'content-type': 'application/json', 'x-news-local': '1' }, body: JSON.stringify({ mode }) });
      setStatus(await readControl(response));
    } catch (failure) { setError(String(failure)); }
  };
  return <>
    <div className="flex flex-wrap items-center gap-3 border-b bg-white px-5 py-3 text-base">
      <label>运行方式：<select aria-label="运行方式" className="rounded border px-3 py-1" value={status.policy === 'auto' ? 'auto' : status.mode} disabled={status.busy} onChange={event => void switchMode(event.target.value)}><option value="auto">自动切换 · 云端优先</option><option value="local">仅本地</option><option value="cloud">仅云端</option><option value="paused">全部暂停</option></select></label>
      <span>当前：{status.busy ? '正在切换' : status.mode === 'cloud' ? '云端' : status.mode === 'local' ? '本地' : '已暂停'}</span>
      <span role="status">{status.message}</span><a className="text-blue-600" href="https://news-ui.example.invalid/" target="_blank" rel="noreferrer">线上备用页面 ↗</a>
      {error && <span role="alert" className="text-red-700">{error}</span>}
    </div><Home localFeatures />
  </>;
}
createRoot(document.getElementById('root')!).render(<App />);
