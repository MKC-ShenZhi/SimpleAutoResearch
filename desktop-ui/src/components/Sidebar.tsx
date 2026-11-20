import { useMemo, useState } from 'react';
import {
  Archive,
  FolderOpen,
  Moon,
  PanelLeftClose,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Sun,
  Trash2,
} from 'lucide-react';
import type { MetaResponse, TaskListItem } from '../types';

interface SidebarProps {
  meta: MetaResponse | null;
  tasks: TaskListItem[];
  activeTask: string | null;
  viewMode: 'new' | 'session' | 'settings';
  dark: boolean;
  onToggleTheme: () => void;
  onNewResearch: () => void;
  onSelectTask: (task: string) => void;
  onArchiveTask: (task: string) => Promise<void>;
  onDeleteTask: (task: string) => Promise<void>;
  onOpenSettings: () => void;
  onRefresh: () => void;
  onCollapse: () => void;
}

function statusMeta(status: string, workerLive?: boolean): { color: string; label: string; pulse?: boolean } {
  if (workerLive) {
    return { color: 'bg-blue-500', label: '运行中', pulse: true };
  }
  switch (status) {
    case 'running':
      return { color: 'bg-amber-400', label: '待继续' };
    case 'completed':
      return { color: 'bg-emerald-500', label: '已完成' };
    case 'partial':
      return { color: 'bg-amber-400', label: '部分交付' };
    case 'paused':
      return { color: 'bg-amber-500', label: '已暂停' };
    case 'blocked':
      return { color: 'bg-orange-500', label: '待处理' };
    case 'failed':
      return { color: 'bg-rose-500', label: '失败' };
    default:
      return { color: 'bg-slate-400', label: status === 'created' ? '待启动' : status || '新建' };
  }
}

export function Sidebar({
  meta,
  tasks,
  activeTask,
  viewMode,
  dark,
  onToggleTheme,
  onNewResearch,
  onSelectTask,
  onArchiveTask,
  onDeleteTask,
  onOpenSettings,
  onRefresh,
  onCollapse,
}: SidebarProps) {
  const [query, setQuery] = useState('');
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tasks;
    return tasks.filter(
      (t) =>
        (t.topic || '').toLowerCase().includes(q) ||
        t.task.toLowerCase().includes(q) ||
        (t.status || '').toLowerCase().includes(q)
    );
  }, [tasks, query]);

  const projectName = useMemo(() => {
    if (!meta?.project) return '本地工作区';
    const parts = meta.project.replace(/\\/g, '/').split('/').filter(Boolean);
    return parts[parts.length - 1] || meta.project;
  }, [meta?.project]);

  const defaultModel = useMemo(() => {
    const found = meta?.profiles.find((p) => p.default) || meta?.profiles[0];
    return found ? found.model : '未配置模型';
  }, [meta?.profiles]);

  return (
    <aside
      className="flex h-full w-[264px] shrink-0 flex-col border-r select-none"
      style={{
        backgroundColor: 'var(--bg-sidebar)',
        borderColor: 'var(--border-subtle)',
      }}
    >
      {/* Top Brand Bar */}
      <div className="flex items-center justify-between px-4 pt-4 pb-3">
        <div className="flex items-center gap-2.5 min-w-0">
          <img
            src="/desktop/logo.svg"
            alt="SimpleAutoResearch"
            className="h-7 w-7 shrink-0 rounded-lg shadow-sm"
          />
          <div className="flex items-baseline gap-1.5 min-w-0">
            <span className="font-semibold tracking-tight text-[15px] truncate">SimpleAutoResearch</span>
          </div>
        </div>
        <button
          onClick={onCollapse}
          title="收起侧栏"
          className="rounded-md p-1.5 transition-colors hover:opacity-100 opacity-70"
          style={{ color: 'var(--text-secondary)' }}
        >
          <PanelLeftClose className="h-4 w-4" />
        </button>
      </div>

      {/* Primary New Research CTA */}
      <div className="px-3 pb-2">
        <button
          onClick={onNewResearch}
          className="flex w-full items-center justify-center gap-2 rounded-xl border px-3 py-2 text-[13.5px] font-medium transition-all cursor-pointer"
          style={{
            backgroundColor: viewMode === 'new' ? 'var(--bg-surface-hover)' : 'var(--bg-surface)',
            borderColor: viewMode === 'new' ? 'var(--border-strong)' : 'var(--border-subtle)',
            color: 'var(--text-primary)',
          }}
        >
          <Plus className="h-4 w-4" />
          <span>新建研究</span>
        </button>
      </div>

      {/* Navigation Links */}
      <div className="px-3 pb-3 space-y-0.5">
        <button
          onClick={onOpenSettings}
          className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-[13px] transition-colors cursor-pointer"
          style={{
            backgroundColor: viewMode === 'settings' ? 'var(--bg-surface-hover)' : 'transparent',
            color: viewMode === 'settings' ? 'var(--text-primary)' : 'var(--text-secondary)',
          }}
        >
          <Settings className="h-4 w-4 shrink-0" />
          <span>模型与连接设置</span>
        </button>
      </div>

      {/* Workspace Section Header & Search */}
      <div className="px-3 pt-1 pb-2">
        <div className="mb-1.5 flex items-center justify-between px-1">
          <div className="flex items-center gap-1.5 text-[11.5px] font-medium" style={{ color: 'var(--text-muted)' }}>
            <FolderOpen className="h-3.5 w-3.5" />
            <span className="truncate max-w-[155px]" title={meta?.project || ''}>
              {projectName}
            </span>
          </div>
          <button
            onClick={onRefresh}
            title="刷新会话列表"
            className="rounded p-1 transition-colors hover:opacity-100 opacity-65 cursor-pointer"
            style={{ color: 'var(--text-secondary)' }}
          >
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </div>

        <div
          className="flex items-center gap-2 rounded-lg border px-2.5 py-1.5"
          style={{
            backgroundColor: 'var(--bg-surface)',
            borderColor: 'var(--border-subtle)',
          }}
        >
          <Search className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--text-muted)' }} />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索研究会话..."
            className="w-full bg-transparent text-[12.5px] outline-none placeholder:opacity-60"
            style={{ color: 'var(--text-primary)' }}
          />
        </div>
      </div>

      {/* Session List */}
      <div className="flex-1 overflow-y-auto px-2 pb-3 space-y-0.5">
        {filtered.length === 0 ? (
          <div className="px-3 py-8 text-center text-[12.5px]" style={{ color: 'var(--text-muted)' }}>
            {tasks.length === 0 ? '暂无历史研究会话' : '没有匹配的会话'}
          </div>
        ) : (
          filtered.map((item) => {
            const selected = viewMode === 'session' && activeTask === item.task;
            const st = statusMeta(item.status, item.worker_live);
            const confirming = confirmDelete === item.task;
            return (
              <div
                key={item.task}
                className="group relative rounded-lg transition-colors"
                style={{
                  backgroundColor: selected ? 'var(--bg-surface-hover)' : 'transparent',
                }}
              >
                <div className="flex items-start gap-2.5 px-2.5 py-2">
                  <button
                    onClick={() => onSelectTask(item.task)}
                    className="flex min-w-0 flex-1 items-start gap-2.5 text-left cursor-pointer"
                  >
                    <span className="relative mt-1.5 flex h-2 w-2 shrink-0" title={st.label}>
                      {st.pulse && (
                        <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-75 ${st.color}`} />
                      )}
                      <span className={`relative inline-flex h-2 w-2 rounded-full ${st.color}`} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div
                        className="truncate text-[13px] font-medium leading-snug"
                        style={{ color: selected ? 'var(--text-primary)' : 'var(--text-secondary)' }}
                      >
                        {item.topic || item.task}
                      </div>
                      <div className="mt-0.5 flex items-center gap-1.5 text-[11px]" style={{ color: 'var(--text-muted)' }}>
                        <span className="shrink-0 whitespace-nowrap">{st.label}</span>
                        <span className="shrink-0">·</span>
                        <span className="truncate font-mono">{item.task}</span>
                      </div>
                    </div>
                  </button>

                  {item.task !== '.' && !item.worker_live && (
                    <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          void onArchiveTask(item.task);
                        }}
                        title="归档会话（移入 .archived/，可随时找回）"
                        className="rounded p-1 hover:bg-white/10 cursor-pointer"
                        style={{ color: 'var(--text-muted)' }}
                      >
                        <Archive className="h-3.5 w-3.5" />
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setConfirmDelete(confirming ? null : item.task);
                        }}
                        title="彻底删除会话"
                        className="rounded p-1 hover:bg-rose-500/15 hover:text-rose-400 cursor-pointer"
                        style={{ color: 'var(--text-muted)' }}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  )}
                </div>

                {confirming && (
                  <div
                    className="mx-2 mb-2 flex items-center justify-between gap-2 rounded-lg border border-rose-500/35 bg-rose-500/10 px-2.5 py-1.5 text-[11.5px]"
                  >
                    <span className="text-rose-400">确认彻底删除？</span>
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => setConfirmDelete(null)}
                        className="rounded px-1.5 py-0.5 cursor-pointer"
                        style={{ color: 'var(--text-secondary)' }}
                      >
                        取消
                      </button>
                      <button
                        onClick={() => {
                          setConfirmDelete(null);
                          void onDeleteTask(item.task);
                        }}
                        className="rounded bg-rose-600 px-2 py-0.5 font-medium text-white hover:bg-rose-500 cursor-pointer"
                      >
                        删除
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* Bottom Footer */}
      <div
        className="flex items-center justify-between border-t px-3 py-2.5"
        style={{ borderColor: 'var(--border-subtle)' }}
      >
        <button
          onClick={onOpenSettings}
          className="flex items-center gap-2 min-w-0 rounded-md px-1.5 py-1 text-left transition-colors hover:opacity-100 opacity-85 cursor-pointer"
          title={`当前默认模型: ${defaultModel}`}
        >
          <span className="h-2 w-2 shrink-0 rounded-full bg-emerald-500" />
          <span className="truncate text-[12px] font-mono" style={{ color: 'var(--text-secondary)' }}>
            {defaultModel}
          </span>
        </button>
        <button
          onClick={onToggleTheme}
          title={dark ? '切换到浅色主题' : '切换到深色主题'}
          className="rounded-lg p-1.5 transition-colors cursor-pointer"
          style={{ color: 'var(--text-secondary)', backgroundColor: 'var(--bg-surface)' }}
        >
          {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>
      </div>
    </aside>
  );
}
