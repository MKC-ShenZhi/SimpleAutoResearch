import { useEffect, useState } from 'react';
import {
  ChevronLeft,
  ChevronRight,
  CopyPlus,
  Download,
  FileCode2,
  FileImage,
  FileText,
  Folder,
  FolderOpen,
  Layers,
  Loader2,
  Receipt,
  X,
} from 'lucide-react';
import { api } from '../api';
import type { DeliveryFile, DeliverySnapshot, FileEntry, TaskSnapshot } from '../types';
import { summarizeResources } from '../utils/resources';
import { MarkdownView } from './MarkdownView';

interface ArtifactDrawerProps {
  snapshot: TaskSnapshot;
  delivery: DeliverySnapshot | null;
  selectedFile: DeliveryFile | null;
  onSelectFile: (file: DeliveryFile | null) => void;
  onReuseVersion: (origin: { from_session: string; reuse_version: number; reuse: string[]; label: string }) => void;
  onClose: () => void;
}

type TabKey = 'deliverables' | 'files' | 'budget';

export function ArtifactDrawer({
  snapshot,
  delivery,
  selectedFile,
  onSelectFile,
  onReuseVersion,
  onClose,
}: ArtifactDrawerProps) {
  const [tab, setTab] = useState<TabKey>('deliverables');

  // File browser state
  const [mount, setMount] = useState<string>('workspace');
  const [currentDir, setCurrentDir] = useState<string>('');
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [loadingDir, setLoadingDir] = useState(false);

  // Preview state
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewKind, setPreviewKind] = useState<'markdown' | 'text' | 'image' | 'pdf' | 'binary' | null>(null);
  const [previewText, setPreviewText] = useState('');
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [pdfPage, setPdfPage] = useState(1);
  const [pdfTotal, setPdfTotal] = useState(1);

  // Load workspace files when tab === 'files'
  useEffect(() => {
    if (tab !== 'files') return;
    let cancelled = false;
    setLoadingDir(true);
    api
      .getFiles(snapshot.task, mount, currentDir)
      .then((rows) => {
        if (!cancelled) setEntries(rows);
      })
      .catch(() => {
        if (!cancelled) setEntries([]);
      })
      .finally(() => {
        if (!cancelled) setLoadingDir(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, snapshot.task, mount, currentDir]);

  // Load file preview when selectedFile changes
  useEffect(() => {
    if (!selectedFile) {
      setPreviewKind(null);
      setPreviewError(null);
      return;
    }
    let cancelled = false;
    let objectUrl: string | null = null;

    const ext = selectedFile.name.toLowerCase().split('.').pop() || '';
    setPreviewLoading(true);
    setPreviewError(null);

    if (ext === 'pdf') {
      setPreviewKind('pdf');
      api
        .fetchPdfPage(snapshot.task, selectedFile.mount, selectedFile.path, pdfPage)
        .then(({ url, totalPages }) => {
          if (cancelled) {
            URL.revokeObjectURL(url);
            return;
          }
          objectUrl = url;
          setPreviewUrl(url);
          setPdfTotal(totalPages);
        })
        .catch((err) => {
          if (!cancelled) setPreviewError(err instanceof Error ? err.message : 'PDF 预览失败');
        })
        .finally(() => {
          if (!cancelled) setPreviewLoading(false);
        });
    } else {
      api
        .fetchFileBlob(snapshot.task, selectedFile.mount, selectedFile.path)
        .then(async ({ blob, contentType }) => {
          if (cancelled) return;
          if (contentType.startsWith('image/') || ['png', 'jpg', 'jpeg', 'webp', 'svg'].includes(ext)) {
            objectUrl = URL.createObjectURL(blob);
            setPreviewUrl(objectUrl);
            setPreviewKind('image');
          } else if (ext === 'md' || ext === 'markdown') {
            setPreviewText(await blob.text());
            setPreviewKind('markdown');
          } else if (contentType.startsWith('text/') || ['json', 'csv', 'py', 'ts', 'tsx', 'js', 'toml', 'yaml', 'yml', 'tex', 'bib', 'txt', 'r'].includes(ext)) {
            setPreviewText(await blob.text());
            setPreviewKind('text');
          } else {
            setPreviewKind('binary');
          }
        })
        .catch((err) => {
          if (!cancelled) setPreviewError(err instanceof Error ? err.message : '读取文件失败');
        })
        .finally(() => {
          if (!cancelled) setPreviewLoading(false);
        });
    }

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [snapshot.task, selectedFile, pdfPage]);

  const handleDownload = async () => {
    if (!selectedFile) return;
    try {
      const { blob } = await api.fetchFileBlob(snapshot.task, selectedFile.mount, selectedFile.path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = selectedFile.name;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // ignore
    }
  };

  const mounts = ['workspace', 'evidence', ...snapshot.sources.filter((s) => snapshot.source_access[s])];
  const resSummary = summarizeResources(snapshot.resources);

  return (
    <aside
      className="flex h-full w-[440px] shrink-0 flex-col border-l"
      style={{
        backgroundColor: 'var(--bg-sidebar)',
        borderColor: 'var(--border-subtle)',
      }}
    >
      {/* Top Tabs */}
      <div
        className="flex items-center justify-between border-b px-3 py-2.5"
        style={{ borderColor: 'var(--border-subtle)' }}
      >
        <div className="flex items-center gap-1">
          {[
            { key: 'deliverables' as TabKey, label: '交付成果', icon: Layers },
            { key: 'files' as TabKey, label: '文件', icon: FolderOpen },
            { key: 'budget' as TabKey, label: '账本', icon: Receipt },
          ].map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => {
                setTab(key);
                onSelectFile(null);
              }}
              className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12.5px] font-medium transition-colors cursor-pointer"
              style={{
                backgroundColor: tab === key && !selectedFile ? 'var(--bg-surface-hover)' : 'transparent',
                color: tab === key && !selectedFile ? 'var(--text-primary)' : 'var(--text-muted)',
              }}
            >
              <Icon className="h-3.5 w-3.5" />
              <span>{label}</span>
            </button>
          ))}
        </div>
        <button
          onClick={onClose}
          title="关闭成果面板"
          className="rounded-md p-1 opacity-70 hover:opacity-100 cursor-pointer"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* File Preview Mode */}
      {selectedFile ? (
        <div className="flex min-h-0 flex-1 flex-col">
          <div
            className="flex items-center justify-between gap-2 border-b px-3.5 py-2"
            style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-surface)' }}
          >
            <button
              onClick={() => {
                onSelectFile(null);
                setPdfPage(1);
              }}
              className="inline-flex items-center gap-1 text-[12px] cursor-pointer"
              style={{ color: 'var(--accent)' }}
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              <span>返回列表</span>
            </button>
            <span className="truncate text-[12.5px] font-medium font-mono" title={selectedFile.path}>
              {selectedFile.name}
            </span>
            <button
              onClick={handleDownload}
              title="下载文件"
              className="rounded p-1 opacity-75 hover:opacity-100 cursor-pointer"
            >
              <Download className="h-3.5 w-3.5" />
            </button>
          </div>

          <div className="flex-1 overflow-auto p-4" style={{ backgroundColor: 'var(--bg-app)' }}>
            {previewLoading ? (
              <div className="flex h-40 items-center justify-center gap-2 text-[13px]" style={{ color: 'var(--text-muted)' }}>
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>加载预览中…</span>
              </div>
            ) : previewError ? (
              <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-[12.5px] text-rose-400">
                {previewError}
              </div>
            ) : previewKind === 'markdown' ? (
              <MarkdownView content={previewText} />
            ) : previewKind === 'text' ? (
              <pre className="whitespace-pre-wrap break-all font-mono text-[12px] leading-relaxed">
                {previewText}
              </pre>
            ) : previewKind === 'image' && previewUrl ? (
              <div className="flex flex-col items-center">
                <img src={previewUrl} alt={selectedFile.name} className="max-w-full rounded-lg border" style={{ borderColor: 'var(--border-subtle)' }} />
              </div>
            ) : previewKind === 'pdf' && previewUrl ? (
              <div className="flex flex-col items-center gap-3">
                <div className="flex items-center gap-3 text-[12.5px]">
                  <button
                    disabled={pdfPage <= 1}
                    onClick={() => setPdfPage((p) => Math.max(1, p - 1))}
                    className="rounded-lg border px-2 py-1 disabled:opacity-40 cursor-pointer"
                    style={{ borderColor: 'var(--border-subtle)' }}
                  >
                    <ChevronLeft className="h-3.5 w-3.5" />
                  </button>
                  <span className="font-mono">
                    第 {pdfPage} / {pdfTotal} 页
                  </span>
                  <button
                    disabled={pdfPage >= pdfTotal}
                    onClick={() => setPdfPage((p) => Math.min(pdfTotal, p + 1))}
                    className="rounded-lg border px-2 py-1 disabled:opacity-40 cursor-pointer"
                    style={{ borderColor: 'var(--border-subtle)' }}
                  >
                    <ChevronRight className="h-3.5 w-3.5" />
                  </button>
                </div>
                <img src={previewUrl} alt={`Page ${pdfPage}`} className="w-full rounded-lg border shadow-sm" style={{ borderColor: 'var(--border-subtle)' }} />
              </div>
            ) : (
              <div className="py-10 text-center text-[13px]" style={{ color: 'var(--text-muted)' }}>
                此二进制文件暂不支持内嵌预览，请点击右上角下载查看。
              </div>
            )}
          </div>
        </div>
      ) : tab === 'deliverables' ? (
        <div className="flex-1 overflow-y-auto p-3.5 space-y-4">
          {delivery?.files && delivery.files.length > 0 ? (
            <div>
              <div className="mb-2 text-[11.5px] font-medium uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>
                已交付文件 ({delivery.files.length})
              </div>
              <div className="space-y-1">
                {delivery.files.map((file) => {
                  const ext = file.name.toLowerCase().split('.').pop() || '';
                  const Icon = ['png', 'jpg', 'jpeg', 'svg', 'webp'].includes(ext)
                    ? FileImage
                    : ['py', 'json', 'csv', 'tex'].includes(ext)
                    ? FileCode2
                    : FileText;
                  return (
                    <button
                      key={`${file.mount}/${file.path}`}
                      onClick={() => {
                        setPdfPage(1);
                        onSelectFile(file);
                      }}
                      className="flex w-full items-center gap-2.5 rounded-xl border px-3 py-2 text-left transition-colors cursor-pointer"
                      style={{
                        backgroundColor: 'var(--bg-surface)',
                        borderColor: 'var(--border-subtle)',
                      }}
                    >
                      <Icon className="h-4 w-4 shrink-0" style={{ color: 'var(--accent)' }} />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-[13px] font-medium">{file.name}</div>
                        <div className="truncate font-mono text-[11px]" style={{ color: 'var(--text-muted)' }}>
                          {file.path}
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          ) : (
            <div className="py-10 text-center text-[13px]" style={{ color: 'var(--text-muted)' }}>
              当前会话暂无可预览的正式交付文件
            </div>
          )}

          {/* Delivery Versions */}
          {delivery?.versions && delivery.versions.length > 0 && (
            <div>
              <div className="mb-2 text-[11.5px] font-medium uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>
                交付版本历史
              </div>
              <div className="space-y-2">
                {delivery.versions.map((ver) => (
                  <div
                    key={ver.number}
                    className="rounded-xl border p-3 text-[12.5px]"
                    style={{
                      backgroundColor: 'var(--bg-surface)',
                      borderColor: 'var(--border-subtle)',
                    }}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-mono font-semibold">v{ver.number} · {ver.status}</span>
                      <span className="text-[11.5px]" style={{ color: 'var(--text-muted)' }}>
                        {ver.file_count} 文件
                      </span>
                    </div>
                    {ver.summary && (
                      <div className="mt-1 line-clamp-2 text-[12px]" style={{ color: 'var(--text-secondary)' }}>
                        {ver.summary}
                      </div>
                    )}
                    <button
                      onClick={() =>
                        onReuseVersion({
                          ...ver.origin,
                          label: snapshot.manifest.topic || snapshot.task,
                        })
                      }
                      className="mt-2 inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[11.5px] font-medium cursor-pointer"
                      style={{ backgroundColor: 'var(--accent-soft)', color: 'var(--accent)' }}
                    >
                      <CopyPlus className="h-3.5 w-3.5" />
                      <span>基于此版本新建研究</span>
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      ) : tab === 'files' ? (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex items-center gap-1.5 overflow-x-auto border-b px-3 py-2" style={{ borderColor: 'var(--border-subtle)' }}>
            {mounts.map((m) => (
              <button
                key={m}
                onClick={() => {
                  setMount(m);
                  setCurrentDir('');
                }}
                className="shrink-0 rounded-md px-2 py-1 text-[11.5px] font-mono cursor-pointer"
                style={{
                  backgroundColor: mount === m ? 'var(--accent-soft)' : 'var(--bg-surface)',
                  color: mount === m ? 'var(--accent)' : 'var(--text-secondary)',
                }}
              >
                {m}
              </button>
            ))}
          </div>

          {currentDir && (
            <button
              onClick={() => {
                const parts = currentDir.split('/').filter(Boolean);
                parts.pop();
                setCurrentDir(parts.join('/'));
              }}
              className="flex items-center gap-1.5 border-b px-3.5 py-2 text-[12px] cursor-pointer"
              style={{ borderColor: 'var(--border-subtle)', color: 'var(--accent)' }}
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              <span className="font-mono">.. ({currentDir})</span>
            </button>
          )}

          <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
            {loadingDir ? (
              <div className="flex justify-center py-8">
                <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--text-muted)' }} />
              </div>
            ) : entries.length === 0 ? (
              <div className="py-8 text-center text-[12.5px]" style={{ color: 'var(--text-muted)' }}>
                空目录
              </div>
            ) : (
              entries.map((entry) => (
                <button
                  key={entry.path || entry.name}
                  onClick={() => {
                    if (entry.directory) {
                      setCurrentDir(entry.path);
                    } else {
                      setPdfPage(1);
                      onSelectFile({ group: mount, mount, path: entry.path, name: entry.name });
                    }
                  }}
                  className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12.5px] transition-colors hover:opacity-100 cursor-pointer"
                  style={{ color: 'var(--text-primary)' }}
                >
                  {entry.directory ? (
                    <Folder className="h-4 w-4 shrink-0 text-amber-400" />
                  ) : (
                    <FileText className="h-4 w-4 shrink-0" style={{ color: 'var(--text-muted)' }} />
                  )}
                  <span className="truncate font-mono">{entry.name}</span>
                </button>
              ))
            )}
          </div>
        </div>
      ) : (
        /* Budget & Ledger Tab */
        <div className="flex-1 overflow-y-auto p-3.5 space-y-3 text-[12.5px]">
          <div
            className="rounded-xl border p-3.5 space-y-2"
            style={{ backgroundColor: 'var(--bg-surface)', borderColor: 'var(--border-subtle)' }}
          >
            <div className="font-semibold">资源用量总览</div>
            <div className="grid grid-cols-2 gap-2 font-mono text-[12px]">
              <div className="rounded-lg p-2" style={{ backgroundColor: 'var(--bg-app)' }}>
                <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>模型调用</div>
                <div className="mt-0.5 font-semibold">
                  {resSummary.llmRequests} / {resSummary.llmLimit ?? '∞'}
                </div>
              </div>
              <div className="rounded-lg p-2" style={{ backgroundColor: 'var(--bg-app)' }}>
                <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>总 Tokens</div>
                <div className="mt-0.5 font-semibold">
                  {resSummary.totalTokens.toLocaleString()}
                </div>
              </div>
              <div className="rounded-lg p-2" style={{ backgroundColor: 'var(--bg-app)' }}>
                <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>进程调用</div>
                <div className="mt-0.5 font-semibold">
                  {resSummary.processInvocations}
                </div>
              </div>
              <div className="rounded-lg p-2" style={{ backgroundColor: 'var(--bg-app)' }}>
                <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>未知预留</div>
                <div className="mt-0.5 font-semibold">
                  {resSummary.unknownCount}
                </div>
              </div>
            </div>
          </div>

          <div
            className="rounded-xl border p-3.5"
            style={{ backgroundColor: 'var(--bg-surface)', borderColor: 'var(--border-subtle)' }}
          >
            <div className="mb-2 font-semibold">
              最近账本记录 ({snapshot.resources?.entries?.length ?? 0})
            </div>
            <div className="space-y-1.5">
              {(snapshot.resources?.entries || []).slice(-15).reverse().map((e) => (
                <div
                  key={e.reservation_id}
                  className="flex items-center justify-between rounded-lg px-2.5 py-1.5 font-mono text-[11.5px]"
                  style={{ backgroundColor: 'var(--bg-app)' }}
                >
                  <span className="truncate max-w-[180px]">{e.purpose || e.attempt_id || e.reservation_id}</span>
                  <span style={{ color: 'var(--text-muted)' }}>
                    {e.status} · {e.actual?.total_tokens ? `${e.actual.total_tokens.toLocaleString()} tok` : ''}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
