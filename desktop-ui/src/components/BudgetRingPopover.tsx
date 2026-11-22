import { useEffect, useRef, useState } from 'react';
import { Loader2, Plus } from 'lucide-react';
import type { ResourceSummary } from '../utils/resources';

interface BudgetRingPopoverProps {
  summary: ResourceSummary;
  workerLive: boolean;
  busy: boolean;
  onAuthorize: (allowances: { llm_requests?: number; total_tokens?: number }) => Promise<void>;
}

function formatCompactTokens(value: number): string {
  if (value >= 1_000_000) {
    const m = value / 1_000_000;
    return `${m % 1 === 0 ? m.toFixed(0) : m.toFixed(1)}M`;
  }
  if (value >= 1_000) {
    return `${Math.round(value / 1_000)}k`;
  }
  return String(Math.round(value));
}

const PRESETS = [
  { label: '+10 轮 / +400k', turns: 10, tokens: 400_000 },
  { label: '+20 轮 / +800k', turns: 20, tokens: 800_000 },
  { label: '+30 轮 / +1.2M', turns: 30, tokens: 1_200_000 },
];

export function BudgetRingPopover({
  summary,
  workerLive,
  busy,
  onAuthorize,
}: BudgetRingPopoverProps) {
  const [pinned, setPinned] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [addTurns, setAddTurns] = useState('15');
  const [addTokens, setAddTokens] = useState('600000');
  const containerRef = useRef<HTMLDivElement>(null);

  const open = pinned || hovered;

  useEffect(() => {
    if (!pinned) return;
    const onPointerDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setPinned(false);
      }
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPinned(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [pinned]);

  const tokenLimit = summary.tokenLimit;
  const llmLimit = summary.llmLimit;
  const tokenUsed = summary.totalTokens;
  const callsUsed = summary.llmRequests;

  const tokenPct = tokenLimit && tokenLimit > 0 ? Math.min(100, Math.round((tokenUsed / tokenLimit) * 100)) : 0;
  const callsPct = llmLimit && llmLimit > 0 ? Math.min(100, Math.round((callsUsed / llmLimit) * 100)) : 0;
  const tokenRemaining = tokenLimit !== null ? Math.max(0, tokenLimit - tokenUsed) : null;
  const callsRemaining = llmLimit !== null ? Math.max(0, llmLimit - callsUsed) : null;

  const maxPct = Math.max(tokenPct, callsPct);
  const exhausted =
    (tokenLimit !== null && tokenRemaining === 0) ||
    (llmLimit !== null && callsRemaining === 0);

  const ringColor =
    exhausted || maxPct >= 90
      ? '#f43f5e'
      : maxPct >= 70
        ? '#f59e0b'
        : 'var(--accent)';

  const statusTag = exhausted
    ? { text: '已耗尽', cls: 'text-rose-400 border-rose-500/40 bg-rose-500/10' }
    : maxPct >= 70
      ? { text: '偏紧', cls: 'text-amber-400 border-amber-500/40 bg-amber-500/10' }
      : { text: '充足', cls: 'text-emerald-400 border-emerald-500/30 bg-emerald-500/10' };

  // SVG circular ring geometry
  const size = 20;
  const stroke = 2.4;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const dashOffset = circumference * (1 - Math.max(0.04, maxPct / 100));

  const submitDelta = async (deltaTurns: number, deltaTokens: number) => {
    const allowances: { llm_requests?: number; total_tokens?: number } = {};
    if (deltaTurns > 0) {
      allowances.llm_requests = (callsRemaining ?? 0) + deltaTurns;
    }
    if (deltaTokens > 0) {
      allowances.total_tokens = (tokenRemaining ?? 0) + deltaTokens;
    }
    if (!allowances.llm_requests && !allowances.total_tokens) return;
    await onAuthorize(allowances);
    setPinned(false);
  };

  const handleCustomSubmit = async () => {
    const t = parseInt(addTurns, 10);
    const tok = parseInt(addTokens, 10);
    await submitDelta(Number.isNaN(t) ? 0 : Math.max(0, t), Number.isNaN(tok) ? 0 : Math.max(0, tok));
  };

  return (
    <div
      ref={containerRef}
      className="relative inline-flex items-center"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <button
        type="button"
        onClick={() => setPinned((v) => !v)}
        aria-label="查看或追加任务预算额度"
        title={`预算已用 ${maxPct}%（点击追加额度）`}
        className="inline-flex h-7 w-7 items-center justify-center rounded-lg border transition-colors cursor-pointer"
        style={{
          borderColor: pinned || exhausted ? ringColor : 'var(--border-subtle)',
          backgroundColor: pinned ? 'var(--bg-surface-hover)' : 'var(--bg-app)',
        }}
      >
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90">
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke="var(--border-strong)"
            strokeWidth={stroke}
            opacity={0.45}
          />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={ringColor}
            strokeWidth={stroke}
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={dashOffset}
          />
        </svg>
      </button>

      {open && (
        <div
          className="absolute bottom-full right-0 z-40 mb-2 w-[288px] rounded-2xl border p-3.5 shadow-xl"
          style={{
            backgroundColor: 'var(--bg-surface)',
            borderColor: 'var(--border-strong)',
            color: 'var(--text-primary)',
          }}
        >
          {/* Header */}
          <div className="mb-2.5 flex items-center justify-between gap-2">
            <span className="text-[12.5px] font-semibold">任务预算额度</span>
            <span className={`rounded-full border px-2 py-0.5 text-[10.5px] font-medium ${statusTag.cls}`}>
              {statusTag.text}
            </span>
          </div>

          {/* Token Meter */}
          <div className="space-y-1">
            <div className="flex items-baseline justify-between text-[12px]">
              <span style={{ color: 'var(--text-secondary)' }}>Token 额度</span>
              <span className="font-mono font-medium">
                {tokenLimit ? `${tokenPct}% 已用（剩余 ${100 - tokenPct}%）` : `${formatCompactTokens(tokenUsed)} 已用`}
              </span>
            </div>
            {tokenLimit !== null && (
              <div className="h-1.5 w-full overflow-hidden rounded-full" style={{ backgroundColor: 'var(--bg-app)' }}>
                <div
                  className="h-full rounded-full transition-all"
                  style={{
                    width: `${tokenPct}%`,
                    backgroundColor: tokenPct >= 90 ? '#f43f5e' : tokenPct >= 70 ? '#f59e0b' : 'var(--accent)',
                  }}
                />
              </div>
            )}
            <div className="text-[11px] font-mono" style={{ color: 'var(--text-muted)' }}>
              已用 {formatCompactTokens(tokenUsed)}
              {tokenLimit !== null ? ` / 共 ${formatCompactTokens(tokenLimit)}` : '（未设上限）'}
            </div>
          </div>

          {/* Turns Meter */}
          <div className="mt-2.5 space-y-1">
            <div className="flex items-baseline justify-between text-[12px]">
              <span style={{ color: 'var(--text-secondary)' }}>模型轮次</span>
              <span className="font-mono font-medium">
                {llmLimit !== null
                  ? `${callsUsed} / ${llmLimit} 轮（剩余 ${callsRemaining}）`
                  : `${callsUsed} 轮`}
              </span>
            </div>
            {llmLimit !== null && (
              <div className="h-1.5 w-full overflow-hidden rounded-full" style={{ backgroundColor: 'var(--bg-app)' }}>
                <div
                  className="h-full rounded-full transition-all"
                  style={{
                    width: `${callsPct}%`,
                    backgroundColor: callsPct >= 90 ? '#f43f5e' : callsPct >= 70 ? '#f59e0b' : 'var(--accent)',
                  }}
                />
              </div>
            )}
          </div>

          {summary.processInvocations > 0 && (
            <div className="mt-2 text-[11px] font-mono" style={{ color: 'var(--text-muted)' }}>
              进程执行：已用 {summary.processInvocations} 次
            </div>
          )}

          {/* Top-up Section */}
          <div className="mt-3 border-t pt-2.5" style={{ borderColor: 'var(--border-subtle)' }}>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-[11.5px] font-medium" style={{ color: 'var(--text-secondary)' }}>
                追加剩余额度（保留原账本）
              </span>
              {workerLive && (
                <span className="text-[10.5px]" style={{ color: 'var(--text-muted)' }}>
                  暂停后可追加
                </span>
              )}
            </div>

            {/* Quick Presets */}
            <div className="mb-2 flex flex-wrap gap-1.5">
              {PRESETS.map((p) => (
                <button
                  key={p.label}
                  type="button"
                  disabled={busy || workerLive}
                  onClick={() => void submitDelta(p.turns, p.tokens)}
                  className="rounded-lg border px-2 py-1 text-[11px] font-mono transition-colors hover:opacity-90 disabled:opacity-40 cursor-pointer"
                  style={{
                    borderColor: 'var(--border-subtle)',
                    backgroundColor: 'var(--bg-app)',
                    color: 'var(--text-primary)',
                  }}
                >
                  {p.label}
                </button>
              ))}
            </div>

            {/* Custom Inputs */}
            <div className="grid grid-cols-2 gap-1.5">
              <label className="flex flex-col gap-0.5">
                <span className="text-[10.5px]" style={{ color: 'var(--text-muted)' }}>
                  + 轮次
                </span>
                <input
                  type="number"
                  min={0}
                  value={addTurns}
                  disabled={busy || workerLive}
                  onChange={(e) => setAddTurns(e.target.value)}
                  onFocus={() => setPinned(true)}
                  className="rounded-lg border px-2 py-1 text-[11.5px] font-mono outline-none disabled:opacity-40"
                  style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-app)' }}
                />
              </label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10.5px]" style={{ color: 'var(--text-muted)' }}>
                  + Token
                </span>
                <input
                  type="number"
                  min={0}
                  step={100000}
                  value={addTokens}
                  disabled={busy || workerLive}
                  onChange={(e) => setAddTokens(e.target.value)}
                  onFocus={() => setPinned(true)}
                  className="rounded-lg border px-2 py-1 text-[11.5px] font-mono outline-none disabled:opacity-40"
                  style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-app)' }}
                />
              </label>
            </div>

            <button
              type="button"
              disabled={busy || workerLive}
              onClick={() => void handleCustomSubmit()}
              className="mt-2 inline-flex w-full items-center justify-center gap-1.5 rounded-xl px-3 py-1.5 text-[12px] font-medium disabled:opacity-40 cursor-pointer"
              style={{ backgroundColor: 'var(--btn-primary-bg)', color: 'var(--btn-primary-fg)' }}
            >
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
              <span>{exhausted ? '追加额度并继续' : '确认追加额度'}</span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
