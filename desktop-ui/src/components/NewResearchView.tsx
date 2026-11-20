import { useEffect, useState } from 'react';
import {
  ArrowUp,
  CheckCircle2,
  Cpu,
  FilePlus2,
  FolderPlus,
  Globe,
  Loader2,
  Paperclip,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Terminal,
  X,
} from 'lucide-react';
import { api, desktopBridge } from '../api';
import type { CreationPreview, MetaResponse } from '../types';

interface ReuseOrigin {
  from_session: string;
  reuse_version: number;
  reuse: string[];
  label: string;
}

interface NewResearchViewProps {
  meta: MetaResponse | null;
  reuseOrigin: ReuseOrigin | null;
  onClearReuse: () => void;
  onCreated: (taskName: string) => void;
  onOpenSettings: () => void;
  onNotify: (msg: string, tone?: 'info' | 'error') => void;
}

const STARTERS = [
  {
    title: '文献调研与对比',
    prompt: '围绕指定研究问题检索并阅读原始论文，比较核心方法、实验条件与反例，输出有原文出处的对比分析与建议。',
  },
  {
    title: '数据分析与科研绘图',
    prompt: '先检查所给数据目录结构与指标含义，执行合理的统计或建模分析，产出可复跑脚本和可读的科研图表。',
  },
  {
    title: '科研代码诊断与修复',
    prompt: '调查目标仓库中的相关实现与复现用例，定位根因并提交干净的补丁与独立回归测试。',
  },
  {
    title: '论文改稿与模板排版',
    prompt: '依据原文和实验结果同步修订现有稿件的摘要、对比表与结论，并按目标模板导出可搬迁重编译的工程。',
  },
];

export function NewResearchView({
  meta,
  reuseOrigin,
  onClearReuse,
  onCreated,
  onOpenSettings,
  onNotify,
}: NewResearchViewProps) {
  const [goal, setGoal] = useState('');
  const [materials, setMaterials] = useState<string[]>([]);
  const [manualPath, setManualPath] = useState('');
  const [showPathInput, setShowPathInput] = useState(false);

  const [selectedSelector, setSelectedSelector] = useState('');
  const [allowExecution, setAllowExecution] = useState(false);
  const [allowNetwork, setAllowNetwork] = useState(false);
  const [requireReview, setRequireReview] = useState(false);

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [maxTurns, setMaxTurns] = useState('');
  const [maxTokens, setMaxTokens] = useState('');

  const [preview, setPreview] = useState<CreationPreview | null>(null);
  const [busy, setBusy] = useState<'preview' | 'create' | null>(null);

  useEffect(() => {
    if (!meta) return;
    if (!selectedSelector && meta.profiles.length > 0) {
      const def = meta.profiles.find((p) => p.default) || meta.profiles[0];
      setSelectedSelector(def.selector);
    }
    if (meta.creation_defaults) {
      setAllowExecution(Boolean(meta.creation_defaults.allow_execution));
      setAllowNetwork(Boolean(meta.creation_defaults.allow_network));
      setRequireReview(Boolean(meta.creation_defaults.independent_review_required));
    }
  }, [meta]);

  const currentProfile = meta?.profiles.find((p) => p.selector === selectedSelector);

  const addPaths = (paths: string[]) => {
    if (!paths.length) return;
    setMaterials((prev) => Array.from(new Set([...prev, ...paths])));
    setPreview(null);
  };

  const handlePickFiles = async () => {
    if (desktopBridge.isNative()) {
      const picked = await desktopBridge.pickFiles();
      addPaths(picked);
    } else {
      setShowPathInput(true);
    }
  };

  const handlePickFolder = async () => {
    if (desktopBridge.isNative()) {
      const picked = await desktopBridge.pickFolder();
      addPaths(picked);
    } else {
      setShowPathInput(true);
    }
  };

  const buildPayload = () => {
    if (!currentProfile) {
      throw new Error('请先在“模型与连接设置”中配置一个可用的文本模型。');
    }
    const payload: Record<string, unknown> = {
      goal: goal.trim(),
      model: currentProfile.selector,
      binding_id: currentProfile.binding_id,
      materials,
      execution: allowExecution,
      sandbox_execution: false,
      network: allowNetwork,
      independent_review_required: requireReview,
    };
    if (reuseOrigin) {
      payload.from_session = reuseOrigin.from_session;
      payload.reuse_version = reuseOrigin.reuse_version;
      payload.reuse = reuseOrigin.reuse;
    }
    if (maxTurns.trim()) payload.max_turns = parseInt(maxTurns.trim(), 10);
    if (maxTokens.trim()) payload.max_tokens = parseInt(maxTokens.trim(), 10);
    return payload;
  };

  const handlePreview = async () => {
    if (!goal.trim()) {
      onNotify('请先输入研究目标。', 'error');
      return;
    }
    try {
      setBusy('preview');
      const res = await api.previewCreate(buildPayload());
      setPreview(res);
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '预览配置失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  const handleCreate = async () => {
    if (!preview) return;
    try {
      setBusy('create');
      const res = await api.createTask({
        ...buildPayload(),
        preview_id: preview.preview_id,
      });
      setPreview(null);
      setGoal('');
      setMaterials([]);
      onClearReuse();
      onCreated(res.task);
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '创建任务失败', 'error');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex h-full flex-col overflow-y-auto px-6 py-8">
      <div className="mx-auto flex w-full max-w-[780px] flex-1 flex-col justify-center">
        {/* Hero Heading */}
        <div className="mb-7 text-center">
          <div
            className="mx-auto mb-3 inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px]"
            style={{
              backgroundColor: 'var(--bg-surface)',
              borderColor: 'var(--border-subtle)',
              color: 'var(--text-secondary)',
            }}
          >
            <Sparkles className="h-3.5 w-3.5" style={{ color: 'var(--accent)' }} />
            <span>任务驱动科研工作台</span>
          </div>
          <h1 className="text-[28px] font-semibold tracking-tight">今天想推进什么研究？</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: 'var(--text-muted)' }}>
            描述你的研究目标并附上相关目录、论文或代码，系统会在授权边界内自主调查、执行与交付。
          </p>
        </div>

        {/* Reuse Origin Banner */}
        {reuseOrigin && (
          <div
            className="mb-3 flex items-center justify-between rounded-xl border px-3.5 py-2.5 text-[12.5px]"
            style={{
              backgroundColor: 'var(--accent-soft)',
              borderColor: 'var(--accent)',
              color: 'var(--text-primary)',
            }}
          >
            <div className="flex items-center gap-2 min-w-0">
              <CheckCircle2 className="h-4 w-4 shrink-0" style={{ color: 'var(--accent)' }} />
              <span className="truncate">
                将复用历史成果：<strong>{reuseOrigin.label}</strong>（v{reuseOrigin.reuse_version}）
              </span>
            </div>
            <button onClick={onClearReuse} className="rounded p-0.5 hover:opacity-100 opacity-70 cursor-pointer">
              <X className="h-4 w-4" />
            </button>
          </div>
        )}

        {/* Composer Card */}
        <div
          className="rounded-2xl border shadow-sm transition-all"
          style={{
            backgroundColor: 'var(--bg-surface)',
            borderColor: 'var(--border-strong)',
          }}
        >
          {/* Textarea */}
          <div className="px-4 pt-3.5 pb-2">
            <textarea
              value={goal}
              onChange={(e) => {
                setGoal(e.target.value);
                if (preview) setPreview(null);
              }}
              onKeyDown={(e) => {
                if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                  e.preventDefault();
                  void handlePreview();
                }
              }}
              rows={4}
              placeholder="描述你想解决的科研问题、需要分析的数据或代码改动要求…（Ctrl + Enter 预览并开始）"
              className="w-full resize-none bg-transparent text-[14.5px] leading-relaxed outline-none placeholder:opacity-55"
              style={{ color: 'var(--text-primary)' }}
            />
          </div>

          {/* Attached Materials Chips */}
          {materials.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-4 pb-2.5">
              {materials.map((p) => {
                const base = p.replace(/\\/g, '/').split('/').pop() || p;
                return (
                  <span
                    key={p}
                    title={p}
                    className="inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12px]"
                    style={{
                      backgroundColor: 'var(--bg-surface-hover)',
                      borderColor: 'var(--border-subtle)',
                      color: 'var(--text-secondary)',
                    }}
                  >
                    <Paperclip className="h-3 w-3 shrink-0" />
                    <span className="max-w-[200px] truncate">{base}</span>
                    <button
                      onClick={() => {
                        setMaterials((prev) => prev.filter((item) => item !== p));
                        setPreview(null);
                      }}
                      className="opacity-60 hover:opacity-100 cursor-pointer"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                );
              })}
            </div>
          )}

          {/* Optional manual path bar (for browser mode or pasting paths) */}
          {showPathInput && (
            <div
              className="mx-4 mb-2.5 flex items-center gap-2 rounded-lg border px-2.5 py-1.5"
              style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-app)' }}
            >
              <input
                value={manualPath}
                onChange={(e) => setManualPath(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && manualPath.trim()) {
                    e.preventDefault();
                    addPaths([manualPath.trim()]);
                    setManualPath('');
                  }
                }}
                placeholder="输入授权目录内的文件或文件夹绝对路径，回车添加..."
                className="flex-1 bg-transparent text-[12.5px] font-mono outline-none"
              />
              <button
                onClick={() => {
                  if (manualPath.trim()) {
                    addPaths([manualPath.trim()]);
                    setManualPath('');
                  }
                }}
                className="rounded px-2 py-0.5 text-[12px] font-medium cursor-pointer"
                style={{ backgroundColor: 'var(--accent-soft)', color: 'var(--accent)' }}
              >
                添加
              </button>
              <button
                onClick={() => setShowPathInput(false)}
                className="opacity-60 hover:opacity-100 cursor-pointer"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          )}

          {/* Advanced Budget Caps Drawer */}
          {showAdvanced && (
            <div
              className="mx-4 mb-2.5 grid grid-cols-2 gap-3 rounded-xl border p-3 text-[12.5px]"
              style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-app)' }}
            >
              <label className="flex flex-col gap-1">
                <span style={{ color: 'var(--text-muted)' }}>最大模型轮次（留空用默认）</span>
                <input
                  type="number"
                  min={1}
                  value={maxTurns}
                  onChange={(e) => {
                    setMaxTurns(e.target.value);
                    setPreview(null);
                  }}
                  placeholder={String(meta?.creation_defaults.max_turns ?? 24)}
                  className="rounded-lg border px-2.5 py-1.5 font-mono text-[12.5px] outline-none"
                  style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-surface)' }}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span style={{ color: 'var(--text-muted)' }}>Token 预算上限（留空用默认）</span>
                <input
                  type="number"
                  min={1000}
                  step={10000}
                  value={maxTokens}
                  onChange={(e) => {
                    setMaxTokens(e.target.value);
                    setPreview(null);
                  }}
                  placeholder={String(meta?.creation_defaults.max_tokens ?? 500000)}
                  className="rounded-lg border px-2.5 py-1.5 font-mono text-[12.5px] outline-none"
                  style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-surface)' }}
                />
              </label>
            </div>
          )}

          {/* Bottom Action Toolbar */}
          <div
            className="flex flex-wrap items-center justify-between gap-2 border-t px-3.5 py-2.5"
            style={{ borderColor: 'var(--border-subtle)' }}
          >
            {/* Left controls */}
            <div className="flex flex-wrap items-center gap-1.5">
              <button
                onClick={handlePickFiles}
                title="添加文件材料"
                className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12.5px] transition-colors cursor-pointer"
                style={{ backgroundColor: 'var(--bg-surface-hover)', color: 'var(--text-secondary)' }}
              >
                <FilePlus2 className="h-3.5 w-3.5" />
                <span>文件</span>
              </button>
              <button
                onClick={handlePickFolder}
                title="添加目录材料"
                className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12.5px] transition-colors cursor-pointer"
                style={{ backgroundColor: 'var(--bg-surface-hover)', color: 'var(--text-secondary)' }}
              >
                <FolderPlus className="h-3.5 w-3.5" />
                <span>目录</span>
              </button>

              <div className="mx-1 h-4 w-px" style={{ backgroundColor: 'var(--border-subtle)' }} />

              <button
                onClick={() => {
                  setAllowExecution((v) => !v);
                  setPreview(null);
                }}
                className="inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12px] transition-colors cursor-pointer"
                style={{
                  backgroundColor: allowExecution ? 'var(--accent-soft)' : 'transparent',
                  borderColor: allowExecution ? 'var(--accent)' : 'var(--border-subtle)',
                  color: allowExecution ? 'var(--accent)' : 'var(--text-muted)',
                }}
              >
                <Terminal className="h-3.5 w-3.5" />
                <span>允许执行</span>
              </button>

              <button
                onClick={() => {
                  setAllowNetwork((v) => !v);
                  setPreview(null);
                }}
                className="inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12px] transition-colors cursor-pointer"
                style={{
                  backgroundColor: allowNetwork ? 'var(--accent-soft)' : 'transparent',
                  borderColor: allowNetwork ? 'var(--accent)' : 'var(--border-subtle)',
                  color: allowNetwork ? 'var(--accent)' : 'var(--text-muted)',
                }}
              >
                <Globe className="h-3.5 w-3.5" />
                <span>联网检索</span>
              </button>

              <button
                onClick={() => {
                  setRequireReview((v) => !v);
                  setPreview(null);
                }}
                className="inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12px] transition-colors cursor-pointer"
                style={{
                  backgroundColor: requireReview ? 'var(--accent-soft)' : 'transparent',
                  borderColor: requireReview ? 'var(--accent)' : 'var(--border-subtle)',
                  color: requireReview ? 'var(--accent)' : 'var(--text-muted)',
                }}
              >
                <ShieldCheck className="h-3.5 w-3.5" />
                <span>独立复核</span>
              </button>

              <button
                onClick={() => setShowAdvanced((v) => !v)}
                title="预算与轮次上限"
                className="rounded-lg p-1.5 transition-colors cursor-pointer"
                style={{
                  backgroundColor: showAdvanced ? 'var(--bg-surface-hover)' : 'transparent',
                  color: 'var(--text-muted)',
                }}
              >
                <SlidersHorizontal className="h-3.5 w-3.5" />
              </button>
            </div>

            {/* Right controls: Model Picker + Submit */}
            <div className="flex items-center gap-2">
              {meta && meta.profiles.length > 0 ? (
                <div
                  className="inline-flex items-center gap-1.5 rounded-lg border px-2 py-1"
                  style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-app)' }}
                >
                  <Cpu className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--text-muted)' }} />
                  <select
                    value={selectedSelector}
                    onChange={(e) => {
                      setSelectedSelector(e.target.value);
                      setPreview(null);
                    }}
                    className="max-w-[190px] bg-transparent text-[12.5px] font-mono outline-none cursor-pointer"
                    style={{ color: 'var(--text-primary)' }}
                  >
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
              ) : (
                <button
                  onClick={onOpenSettings}
                  className="rounded-lg border px-2.5 py-1 text-[12px] text-amber-500 border-amber-500/40 cursor-pointer"
                >
                  去配置模型
                </button>
              )}

              <button
                onClick={handlePreview}
                disabled={busy !== null || !goal.trim()}
                className="inline-flex items-center gap-1.5 rounded-xl px-3.5 py-1.5 text-[13px] font-medium transition-opacity disabled:opacity-35 cursor-pointer"
                style={{ backgroundColor: 'var(--btn-primary-bg)', color: 'var(--btn-primary-fg)' }}
              >
                {busy === 'preview' ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <ArrowUp className="h-4 w-4" />
                )}
                <span>预览并开始</span>
              </button>
            </div>
          </div>
        </div>

        {/* Inline Creation Preview & Confirmation Card */}
        {preview && (
          <div
            className="mt-4 rounded-2xl border p-4"
            style={{
              backgroundColor: 'var(--bg-surface)',
              borderColor: 'var(--accent)',
            }}
          >
            <div className="flex items-center justify-between">
              <div className="text-[13.5px] font-semibold">确认任务生效配置</div>
              <button
                onClick={() => setPreview(null)}
                className="text-[12px] opacity-70 hover:opacity-100 cursor-pointer"
              >
                取消
              </button>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-[12.5px]">
              <div>
                <span style={{ color: 'var(--text-muted)' }}>模型连接：</span>
                <span className="font-mono">{preview.model}</span>
              </div>
              <div>
                <span style={{ color: 'var(--text-muted)' }}>权限边界：</span>
                <span>
                  {preview.access.allow_execution ? '允许执行提案' : '仅只读'} ·{' '}
                  {preview.access.allow_network ? '联网' : '离线'} ·{' '}
                  {preview.independent_review_required ? '要求独立复核' : '按需复核'}
                </span>
              </div>
              <div>
                <span style={{ color: 'var(--text-muted)' }}>预算上限：</span>
                <span className="font-mono">
                  {preview.limits.llm_requests ?? '默认'} 次调用 / {preview.limits.total_tokens?.toLocaleString() ?? '默认'} tokens
                </span>
              </div>
              <div>
                <span style={{ color: 'var(--text-muted)' }}>挂载材料：</span>
                <span>{Object.keys(preview.sources || {}).length} 项</span>
              </div>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setPreview(null)}
                className="rounded-lg border px-3 py-1.5 text-[12.5px] cursor-pointer"
                style={{ borderColor: 'var(--border-subtle)', color: 'var(--text-secondary)' }}
              >
                返回修改
              </button>
              <button
                onClick={handleCreate}
                disabled={busy === 'create'}
                className="inline-flex items-center gap-1.5 rounded-lg px-4 py-1.5 text-[12.5px] font-medium cursor-pointer"
                style={{ backgroundColor: 'var(--btn-primary-bg)', color: 'var(--btn-primary-fg)' }}
              >
                {busy === 'create' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                <span>确认创建会话</span>
              </button>
            </div>
          </div>
        )}

        {/* Quick Starter Chips */}
        {!preview && (
          <div className="mt-6 grid grid-cols-2 gap-2.5">
            {STARTERS.map((item) => (
              <button
                key={item.title}
                onClick={() => setGoal(item.prompt)}
                className="rounded-xl border p-3 text-left transition-colors cursor-pointer"
                style={{
                  backgroundColor: 'var(--bg-surface)',
                  borderColor: 'var(--border-subtle)',
                }}
              >
                <div className="text-[13px] font-medium">{item.title}</div>
                <div className="mt-1 line-clamp-2 text-[12px]" style={{ color: 'var(--text-muted)' }}>
                  {item.prompt}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
