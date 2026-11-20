import { useState } from 'react';
import {
  AlertTriangle,
  ArrowUp,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Cpu,
  FileText,
  Globe,
  Loader2,
  PanelRight,
  Pause,
  Play,
  ShieldAlert,
  ShieldCheck,
  Square,
  Terminal,
  Wrench,
  XCircle,
} from 'lucide-react';
import { api } from '../api';
import type { DeliveryFile, DeliverySnapshot, MetaResponse, TaskSnapshot } from '../types';
import { MarkdownView } from './MarkdownView';

interface SessionViewProps {
  meta: MetaResponse | null;
  snapshot: TaskSnapshot;
  delivery: DeliverySnapshot | null;
  drawerOpen: boolean;
  onToggleDrawer: () => void;
  onSelectArtifact: (file: DeliveryFile) => void;
  onReload: () => Promise<void>;
  onNotify: (msg: string, tone?: 'info' | 'error') => void;
}

function formatStatus(status: string, live: boolean): { text: string; cls: string } {
  if (live) return { text: '运行中', cls: 'bg-blue-500/15 text-blue-400 border-blue-500/30' };
  switch (status) {
    case 'running':
      return { text: '待继续', cls: 'bg-amber-500/15 text-amber-400 border-amber-500/30' };
    case 'created':
      return { text: '待启动', cls: 'bg-slate-500/15 text-slate-400 border-slate-500/30' };
    case 'completed':
      return { text: '已完成', cls: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30' };
    case 'partial':
      return { text: '部分交付', cls: 'bg-amber-500/15 text-amber-400 border-amber-500/30' };
    case 'paused':
      return { text: '已暂停', cls: 'bg-amber-500/15 text-amber-400 border-amber-500/30' };
    case 'blocked':
      return { text: '待审批 / 阻塞', cls: 'bg-orange-500/15 text-orange-400 border-orange-500/30' };
    case 'failed':
      return { text: '失败', cls: 'bg-rose-500/15 text-rose-400 border-rose-500/30' };
    default:
      return { text: status, cls: 'bg-slate-500/15 text-slate-400 border-slate-500/30' };
  }
}

export function SessionView({
  meta,
  snapshot,
  delivery,
  drawerOpen,
  onToggleDrawer,
  onSelectArtifact,
  onReload,
  onNotify,
}: SessionViewProps) {
  const [feedback, setFeedback] = useState('');
  const [requireReview, setRequireReview] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const [toolsExpanded, setToolsExpanded] = useState(false);
  const [expandedToolIdx, setExpandedToolIdx] = useState<number | null>(null);

  const [rememberExec, setRememberExec] = useState(false);
  const [rejectReason, setRejectReason] = useState('');
  const [showRejectBox, setShowRejectBox] = useState(false);

  const statusBadge = formatStatus(snapshot.manifest.status, snapshot.worker_live);
  const usedTokens = snapshot.resources?.used?.total_tokens ?? 0;
  const usedCalls = snapshot.resources?.used?.llm_requests ?? 0;

  const handlePause = async () => {
    try {
      setBusy('stop');
      const res = await api.stopTask(snapshot.task);
      onNotify(res.message || '已请求在安全边界暂停');
      await onReload();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '暂停失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const handleResume = async () => {
    try {
      setBusy('resume');
      const res = await api.resumeTask(snapshot.task);
      onNotify(res.message || '已恢复执行');
      await onReload();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '恢复失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const handleApprove = async () => {
    if (!snapshot.approval) return;
    try {
      setBusy('approve');
      await api.approveExecution(snapshot.task, snapshot.approval.token, rememberExec);
      setRememberExec(false);
      onNotify('已批准执行，任务继续推进');
      await onReload();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '批准失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const handleReject = async () => {
    if (!snapshot.approval) return;
    try {
      setBusy('reject');
      await api.rejectExecution(snapshot.task, snapshot.approval.token, rejectReason.trim());
      setRejectReason('');
      setShowRejectBox(false);
      onNotify('已拒绝该命令提案');
      await onReload();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '拒绝失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const handleGrantAccess = async (kind: 'execution' | 'network') => {
    try {
      setBusy(`grant-${kind}`);
      await api.grantAccess(
        snapshot.task,
        kind === 'execution'
          ? { allow_execution: true, auto_approve: true }
          : { allow_network: true }
      );
      onNotify(kind === 'execution' ? '已为本会话开启允许执行（自动批准）' : '已为本会话开启联网检索');
      await onReload();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '开启权限失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const handleToggleAutoApprove = async () => {
    try {
      setBusy('auto-approve');
      const next = !snapshot.auto_approve;
      await api.setAutoApprove(snapshot.task, next);
      onNotify(next ? '已开启命令自动批准' : '已改为手动逐条审批');
      await onReload();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '切换审批模式失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const handleSendFeedback = async () => {
    if (!feedback.trim()) return;
    try {
      setBusy('feedback');
      const res = await api.sendFeedback(snapshot.task, feedback.trim(), requireReview);
      setFeedback('');
      onNotify(res.message || '反馈已提交');
      await onReload();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '提交反馈失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const handleSwitchModel = async (targetSelector: string) => {
    const profile = meta?.profiles.find((p) => p.selector === targetSelector);
    if (!profile || targetSelector === snapshot.model) return;
    try {
      setBusy('switch');
      await api.switchModel(snapshot.task, profile.selector, profile.binding_id, '桌面端切换模型');
      onNotify(`已切换会话模型至 ${profile.model}`);
      await onReload();
    } catch (err) {
      const msg = err instanceof Error ? err.message : '切换模型失败';
      if (msg.includes('Reconcile pending')) {
        onNotify('当前会话存在未结算的失败/待处理调用，按账本安全规则暂不可直接换模；建议新建研究会话使用新模型。', 'error');
      } else {
        onNotify(msg, 'error');
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      {/* Top Sticky Header */}
      <header
        className="flex shrink-0 items-center justify-between gap-3 border-b px-5 py-3"
        style={{
          backgroundColor: 'var(--bg-app)',
          borderColor: 'var(--border-subtle)',
        }}
      >
        <div className="flex items-center gap-2.5 min-w-0">
          <h2 className="truncate text-[15px] font-semibold" title={snapshot.manifest.topic || snapshot.task}>
            {snapshot.manifest.topic || snapshot.task}
          </h2>
          <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11.5px] font-medium ${statusBadge.cls}`}>
            {statusBadge.text}
          </span>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {/* Token & Calls Mini Pill */}
          <div
            className="hidden sm:inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11.5px] font-mono"
            style={{
              backgroundColor: 'var(--bg-surface)',
              borderColor: 'var(--border-subtle)',
              color: 'var(--text-muted)',
            }}
            title="已结算模型调用次数与 Token 用量"
          >
            <span>{usedCalls} 次</span>
            <span>·</span>
            <span>{usedTokens.toLocaleString()} tok</span>
          </div>

          {/* Pause / Resume Button */}
          {snapshot.worker_live ? (
            <button
              onClick={handlePause}
              disabled={busy === 'stop' || snapshot.stop_requested}
              className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-[12.5px] font-medium text-amber-400 cursor-pointer disabled:opacity-50"
            >
              <Pause className="h-3.5 w-3.5" />
              <span>{snapshot.stop_requested ? '暂停中…' : '暂停'}</span>
            </button>
          ) : snapshot.manifest.status !== 'completed' ? (
            <button
              onClick={handleResume}
              disabled={busy === 'resume'}
              className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[12.5px] font-medium cursor-pointer disabled:opacity-50"
              style={{ backgroundColor: 'var(--btn-primary-bg)', color: 'var(--btn-primary-fg)' }}
            >
              {busy === 'resume' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
              <span>继续</span>
            </button>
          ) : null}

          {/* Toggle Right Artifact Drawer */}
          <button
            onClick={onToggleDrawer}
            title={drawerOpen ? '收起成果与文件面板' : '展开成果与文件面板'}
            className="inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12.5px] transition-colors cursor-pointer"
            style={{
              backgroundColor: drawerOpen ? 'var(--accent-soft)' : 'var(--bg-surface)',
              borderColor: drawerOpen ? 'var(--accent)' : 'var(--border-subtle)',
              color: drawerOpen ? 'var(--accent)' : 'var(--text-secondary)',
            }}
          >
            <PanelRight className="h-3.5 w-3.5" />
            <span className="hidden md:inline">成果</span>
          </button>
        </div>
      </header>

      {/* Main Conversation & Activity Stream */}
      <div className="flex-1 overflow-y-auto px-5 py-6">
        <div className="mx-auto max-w-[780px] space-y-5">
          {/* Worker Error Banner */}
          {snapshot.worker_error && (
            <div className="flex items-start gap-2.5 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-[13px] text-rose-400">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{snapshot.worker_error}</span>
            </div>
          )}

          {/* User Requests Bubbles */}
          {snapshot.requests.map((req, idx) => (
            <div key={idx} className="flex justify-end">
              <div
                className="max-w-[85%] rounded-2xl rounded-tr-sm border px-4 py-2.5 text-[14px] leading-relaxed"
                style={{
                  backgroundColor: 'var(--bg-surface-hover)',
                  borderColor: 'var(--border-subtle)',
                }}
              >
                <div className="mb-1 text-[11px] font-medium" style={{ color: 'var(--text-muted)' }}>
                  {idx === 0 ? '初始研究目标' : `追问与反馈 #${idx}`}
                </div>
                <div className="whitespace-pre-wrap">{req}</div>
              </div>
            </div>
          ))}

          {/* Status Reason Note */}
          {snapshot.manifest.status_reason && (
            <div
              className="rounded-xl border px-4 py-2.5 text-[12.5px]"
              style={{
                backgroundColor: 'var(--bg-surface)',
                borderColor: 'var(--border-subtle)',
                color: 'var(--text-secondary)',
              }}
            >
              <span className="font-medium" style={{ color: 'var(--text-primary)' }}>
                当前状态说明：
              </span>
              {snapshot.manifest.status_reason}
            </div>
          )}

          {/* Collapsible Tool Activity Timeline (OpenResearch / DeepSeek style) */}
          {snapshot.activity_total > 0 && (
            <div
              className="rounded-2xl border"
              style={{
                backgroundColor: 'var(--bg-surface)',
                borderColor: 'var(--border-subtle)',
              }}
            >
              <button
                onClick={() => setToolsExpanded((v) => !v)}
                className="flex w-full items-center justify-between px-4 py-3 text-left text-[13px] cursor-pointer"
              >
                <div className="flex items-center gap-2">
                  <Wrench className="h-4 w-4" style={{ color: 'var(--text-muted)' }} />
                  <span className="font-medium">
                    已执行 {snapshot.activity_total} 项工具操作
                    {snapshot.activity.length < snapshot.activity_total
                      ? `（展示最近 ${snapshot.activity.length} 项）`
                      : ''}
                  </span>
                </div>
                {toolsExpanded ? (
                  <ChevronDown className="h-4 w-4" style={{ color: 'var(--text-muted)' }} />
                ) : (
                  <ChevronRight className="h-4 w-4" style={{ color: 'var(--text-muted)' }} />
                )}
              </button>

              {toolsExpanded && (
                <div className="border-t divide-y" style={{ borderColor: 'var(--border-subtle)' }}>
                  {snapshot.activity.map((act, i) => {
                    const isOpen = expandedToolIdx === i;
                    const ok = act.status === 'ok' || act.status === 'completed';
                    return (
                      <div key={i} className="px-4 py-2.5 text-[12.5px]" style={{ borderColor: 'var(--border-subtle)' }}>
                        <button
                          onClick={() => setExpandedToolIdx(isOpen ? null : i)}
                          className="flex w-full items-center justify-between gap-2 text-left cursor-pointer"
                        >
                          <div className="flex items-center gap-2 min-w-0">
                            <span
                              className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                                ok ? 'bg-emerald-500' : act.error ? 'bg-rose-500' : 'bg-amber-400'
                              }`}
                            />
                            <span className="font-mono font-medium">{act.tool || 'action'}</span>
                            {act.summary && (
                              <span className="truncate" style={{ color: 'var(--text-muted)' }}>
                                — {act.summary}
                              </span>
                            )}
                          </div>
                          <span className="shrink-0 font-mono text-[11px]" style={{ color: 'var(--text-muted)' }}>
                            {act.attempt || ''}
                          </span>
                        </button>
                        {act.error && (
                          <div className="mt-1.5 rounded-lg bg-rose-500/10 px-2.5 py-1.5 text-[12px] text-rose-400">
                            {act.error}
                          </div>
                        )}
                        {isOpen && (
                          <pre
                            className="mt-2 max-h-60 overflow-auto rounded-lg p-2.5 text-[11.5px] font-mono"
                            style={{ backgroundColor: 'var(--bg-app)', color: 'var(--text-secondary)' }}
                          >
                            {JSON.stringify(
                              { arguments: act.arguments, observed: act.observed },
                              null,
                              2
                            )}
                          </pre>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {/* Pending Execution Approval Card */}
          {snapshot.approval && (
            <div className="rounded-2xl border border-amber-500/50 bg-amber-500/5 p-4">
              <div className="flex items-center gap-2 text-[14px] font-semibold text-amber-400">
                <ShieldAlert className="h-4.5 w-4.5" />
                <span>等待批准执行命令</span>
              </div>
              {snapshot.approval.execution ? (
                <div className="mt-3 space-y-2 text-[12.5px]">
                  <div
                    className="rounded-xl border p-3 font-mono text-[12px] overflow-x-auto"
                    style={{ backgroundColor: 'var(--bg-app)', borderColor: 'var(--border-subtle)' }}
                  >
                    <div className="flex items-center gap-1.5 mb-1 text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      <Terminal className="h-3 w-3" />
                      <span>cwd: {snapshot.approval.execution.cwd}</span>
                      <span>· timeout: {snapshot.approval.execution.timeout_sec}s</span>
                    </div>
                    <div className="select-all">{snapshot.approval.execution.argv.join(' ')}</div>
                  </div>
                </div>
              ) : (
                <pre
                  className="mt-2.5 overflow-x-auto rounded-xl p-3 text-[12px] font-mono"
                  style={{ backgroundColor: 'var(--bg-app)' }}
                >
                  {snapshot.approval.call?.function?.name}: {snapshot.approval.call?.function?.arguments}
                </pre>
              )}

              <div className="mt-3.5 flex flex-wrap items-center justify-between gap-3">
                <label className="inline-flex items-center gap-2 text-[12.5px] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={rememberExec}
                    onChange={(e) => setRememberExec(e.target.checked)}
                    className="rounded"
                  />
                  <span style={{ color: 'var(--text-secondary)' }}>本会话后续相同范围免重复确认</span>
                </label>

                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setShowRejectBox((v) => !v)}
                    disabled={busy !== null}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/40 px-3 py-1.5 text-[12.5px] font-medium text-rose-400 cursor-pointer"
                  >
                    <XCircle className="h-3.5 w-3.5" />
                    <span>拒绝</span>
                  </button>
                  <button
                    onClick={handleApprove}
                    disabled={busy !== null}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3.5 py-1.5 text-[12.5px] font-medium text-white hover:bg-emerald-500 cursor-pointer"
                  >
                    {busy === 'approve' ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <CheckCircle2 className="h-3.5 w-3.5" />
                    )}
                    <span>批准执行</span>
                  </button>
                </div>
              </div>

              {showRejectBox && (
                <div className="mt-3 flex items-center gap-2">
                  <input
                    value={rejectReason}
                    onChange={(e) => setRejectReason(e.target.value)}
                    placeholder="填写拒绝原因（会反馈给模型调整方案）..."
                    className="flex-1 rounded-lg border px-3 py-1.5 text-[12.5px] outline-none"
                    style={{ backgroundColor: 'var(--bg-surface)', borderColor: 'var(--border-subtle)' }}
                  />
                  <button
                    onClick={handleReject}
                    disabled={busy === 'reject'}
                    className="rounded-lg bg-rose-600 px-3 py-1.5 text-[12.5px] font-medium text-white cursor-pointer"
                  >
                    提交拒绝
                  </button>
                </div>
              )}
            </div>
          )}

          {/* Delivery Summary Card */}
          {delivery && (delivery.summary || delivery.files.length > 0) && (
            <div
              className="rounded-2xl border p-5"
              style={{
                backgroundColor: 'var(--bg-surface)',
                borderColor: delivery.current ? 'var(--border-strong)' : 'var(--border-subtle)',
              }}
            >
              <div className="mb-3 flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <CheckCircle2
                    className={`h-4.5 w-4.5 ${delivery.current ? 'text-emerald-500' : 'text-amber-400'}`}
                  />
                  <span className="text-[14px] font-semibold">
                    {delivery.current ? '正式交付摘要' : '历史交付记录'}
                  </span>
                </div>
                <span className="text-[11.5px]" style={{ color: 'var(--text-muted)' }}>
                  {delivery.files.length} 项可预览成果
                </span>
              </div>

              {delivery.summary && <MarkdownView content={delivery.summary} />}

              {delivery.unresolved.length > 0 && (
                <div className="mt-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 text-[12.5px]">
                  <div className="mb-1 font-medium text-amber-400">未决项</div>
                  <ul className="list-disc pl-4 space-y-0.5" style={{ color: 'var(--text-secondary)' }}>
                    {delivery.unresolved.map((u, i) => (
                      <li key={i}>{u}</li>
                    ))}
                  </ul>
                </div>
              )}

              {delivery.files.length > 0 && (
                <div className="mt-4 flex flex-wrap gap-2">
                  {delivery.files.slice(0, 8).map((f) => (
                    <button
                      key={`${f.mount}/${f.path}`}
                      onClick={() => onSelectArtifact(f)}
                      className="inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[12px] transition-colors cursor-pointer"
                      style={{
                        backgroundColor: 'var(--bg-app)',
                        borderColor: 'var(--border-subtle)',
                        color: 'var(--text-primary)',
                      }}
                    >
                      <FileText className="h-3.5 w-3.5" style={{ color: 'var(--accent)' }} />
                      <span className="max-w-[200px] truncate">{f.name}</span>
                    </button>
                  ))}
                  {delivery.files.length > 8 && (
                    <button
                      onClick={onToggleDrawer}
                      className="rounded-lg px-2.5 py-1.5 text-[12px] cursor-pointer"
                      style={{ color: 'var(--accent)' }}
                    >
                      查看全部 {delivery.files.length} 项 →
                    </button>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Running Indicator */}
          {snapshot.worker_live && (
            <div
              className="flex items-center justify-between rounded-xl border px-4 py-2.5 text-[13px]"
              style={{
                backgroundColor: 'var(--bg-surface)',
                borderColor: 'var(--border-subtle)',
                color: 'var(--text-secondary)',
              }}
            >
              <div className="flex items-center gap-2">
                <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--accent)' }} />
                <span>
                  {snapshot.stop_requested
                    ? '已请求打断，正在当前操作边界安全保存并暂停…'
                    : '研究任务正在推进中，界面将自动刷新最新回执与成果…'}
                </span>
              </div>
              {!snapshot.stop_requested && (
                <button
                  onClick={handlePause}
                  disabled={busy === 'stop'}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 px-2.5 py-1 text-[12px] font-medium text-rose-400 hover:bg-rose-500/20 cursor-pointer"
                >
                  <Square className="h-3 w-3 fill-current" />
                  <span>打断暂停</span>
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Bottom Sticky Feedback Composer */}
      <div className="shrink-0 border-t px-5 py-3" style={{ borderColor: 'var(--border-subtle)' }}>
        <div
          className="mx-auto max-w-[780px] rounded-2xl border"
          style={{
            backgroundColor: 'var(--bg-surface)',
            borderColor: 'var(--border-strong)',
          }}
        >
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                e.preventDefault();
                void handleSendFeedback();
              }
            }}
            rows={2}
            placeholder="补充修改意见、追问或下一步指令…（Ctrl + Enter 发送，只重做受影响部分）"
            className="w-full resize-none bg-transparent px-4 pt-3 pb-1 text-[13.5px] outline-none placeholder:opacity-55"
          />
          <div className="flex flex-wrap items-center justify-between gap-2 px-3.5 pb-2.5 pt-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <button
                onClick={() => {
                  if (!snapshot.access?.allow_execution) void handleGrantAccess('execution');
                }}
                disabled={busy !== null || snapshot.worker_live || Boolean(snapshot.access?.allow_execution)}
                title={snapshot.access?.allow_execution ? '本会话已授予执行权限' : '为本会话开启命令执行权限'}
                className="inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[12px] transition-colors cursor-pointer disabled:cursor-default"
                style={{
                  backgroundColor: snapshot.access?.allow_execution ? 'var(--accent-soft)' : 'transparent',
                  borderColor: snapshot.access?.allow_execution ? 'var(--accent)' : 'var(--border-subtle)',
                  color: snapshot.access?.allow_execution ? 'var(--accent)' : 'var(--text-muted)',
                }}
              >
                <Terminal className="h-3.5 w-3.5" />
                <span>允许执行</span>
              </button>

              {snapshot.access?.allow_execution && (
                <button
                  onClick={() => void handleToggleAutoApprove()}
                  disabled={busy !== null}
                  title="切换是否自动批准授权范围内的命令执行"
                  className="inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[12px] transition-colors cursor-pointer"
                  style={{
                    backgroundColor: snapshot.auto_approve ? 'var(--accent-soft)' : 'transparent',
                    borderColor: snapshot.auto_approve ? 'var(--accent)' : 'var(--border-subtle)',
                    color: snapshot.auto_approve ? 'var(--accent)' : 'var(--text-muted)',
                  }}
                >
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  <span>自动批准</span>
                </button>
              )}

              <button
                onClick={() => {
                  if (!snapshot.access?.allow_network) void handleGrantAccess('network');
                }}
                disabled={busy !== null || snapshot.worker_live || Boolean(snapshot.access?.allow_network)}
                title={snapshot.access?.allow_network ? '本会话已开启联网检索' : '为本会话开启联网检索权限'}
                className="inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[12px] transition-colors cursor-pointer disabled:cursor-default"
                style={{
                  backgroundColor: snapshot.access?.allow_network ? 'var(--accent-soft)' : 'transparent',
                  borderColor: snapshot.access?.allow_network ? 'var(--accent)' : 'var(--border-subtle)',
                  color: snapshot.access?.allow_network ? 'var(--accent)' : 'var(--text-muted)',
                }}
              >
                <Globe className="h-3.5 w-3.5" />
                <span>联网检索</span>
              </button>

              <button
                onClick={() => setRequireReview((v) => !v)}
                className="inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[12px] transition-colors cursor-pointer"
                style={{
                  backgroundColor: requireReview ? 'var(--accent-soft)' : 'transparent',
                  borderColor: requireReview ? 'var(--accent)' : 'var(--border-subtle)',
                  color: requireReview ? 'var(--accent)' : 'var(--text-muted)',
                }}
              >
                <ShieldCheck className="h-3.5 w-3.5" />
                <span>独立复核</span>
              </button>
            </div>
            <div className="flex items-center gap-2">
              {meta && meta.profiles.length > 0 && (
                <div
                  className="inline-flex items-center gap-1.5 rounded-lg border px-2 py-1"
                  style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-app)' }}
                  title="切换当前会话模型（保留原账本）"
                >
                  <Cpu className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--text-muted)' }} />
                  <select
                    value={snapshot.model || ''}
                    disabled={busy === 'switch' || snapshot.worker_live}
                    onChange={(e) => void handleSwitchModel(e.target.value)}
                    className="max-w-[180px] bg-transparent text-[12px] font-mono outline-none cursor-pointer disabled:opacity-50"
                    style={{ color: 'var(--text-primary)' }}
                  >
                    {snapshot.model && !meta.profiles.some((p) => p.selector === snapshot.model) && (
                      <option
                        value={snapshot.model}
                        style={{ backgroundColor: 'var(--bg-surface)', color: 'var(--text-primary)' }}
                      >
                        {snapshot.model}
                      </option>
                    )}
                    {meta.profiles.map((p) => (
                      <option
                        key={p.selector}
                        value={p.selector}
                        style={{ backgroundColor: 'var(--bg-surface)', color: 'var(--text-primary)' }}
                      >
                        {p.model} ({p.selector.replace('profile:', '')})
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {snapshot.worker_live && !feedback.trim() ? (
                <button
                  onClick={handlePause}
                  disabled={busy === 'stop' || snapshot.stop_requested}
                  title="在安全边界打断并暂停当前任务"
                  className="inline-flex items-center gap-1.5 rounded-xl border border-rose-500/40 bg-rose-500/15 px-3.5 py-1.5 text-[12.5px] font-medium text-rose-400 hover:bg-rose-500/25 disabled:opacity-50 cursor-pointer"
                >
                  <Square className="h-3.5 w-3.5 fill-current" />
                  <span>{snapshot.stop_requested ? '暂停中…' : '停止'}</span>
                </button>
              ) : (
                <button
                  onClick={handleSendFeedback}
                  disabled={busy === 'feedback' || !feedback.trim()}
                  className="inline-flex items-center gap-1.5 rounded-xl px-3.5 py-1.5 text-[12.5px] font-medium disabled:opacity-35 cursor-pointer"
                  style={{ backgroundColor: 'var(--btn-primary-bg)', color: 'var(--btn-primary-fg)' }}
                >
                  {busy === 'feedback' ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <ArrowUp className="h-3.5 w-3.5" />
                  )}
                  <span>发送反馈</span>
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
