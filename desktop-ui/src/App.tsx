import { useCallback, useEffect, useState } from 'react';
import { PanelLeftOpen, X } from 'lucide-react';
import { api } from './api';
import { ArtifactDrawer } from './components/ArtifactDrawer';
import { NewResearchView } from './components/NewResearchView';
import { SessionView } from './components/SessionView';
import { SettingsView } from './components/SettingsView';
import { Sidebar } from './components/Sidebar';
import type { DeliveryFile, DeliverySnapshot, MetaResponse, TaskListItem, TaskSnapshot } from './types';

type ViewMode = 'new' | 'session' | 'settings';

export function App() {
  const [dark, setDark] = useState(() => localStorage.getItem('sar_theme') !== 'light');
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const [viewMode, setViewMode] = useState<ViewMode>('new');
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [activeTask, setActiveTask] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<TaskSnapshot | null>(null);
  const [delivery, setDelivery] = useState<DeliverySnapshot | null>(null);
  const [selectedFile, setSelectedFile] = useState<DeliveryFile | null>(null);

  const [reuseOrigin, setReuseOrigin] = useState<{
    from_session: string;
    reuse_version: number;
    reuse: string[];
    label: string;
  } | null>(null);

  const [toast, setToast] = useState<{ msg: string; tone: 'info' | 'error' } | null>(null);

  const notify = useCallback((msg: string, tone: 'info' | 'error' = 'info') => {
    setToast({ msg, tone });
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark);
    localStorage.setItem('sar_theme', dark ? 'dark' : 'light');
  }, [dark]);

  const loadMetaAndTasks = useCallback(async () => {
    try {
      const [m, list] = await Promise.all([api.getMeta(), api.getTasks()]);
      setMeta(m);
      setTasks(list);
    } catch (err) {
      notify(err instanceof Error ? err.message : '无法连接本地工作区服务', 'error');
    }
  }, [notify]);

  const loadActiveSession = useCallback(
    async (name: string) => {
      try {
        const [snap, del] = await Promise.all([
          api.getTask(name),
          api.getDelivery(name).catch(() => null),
        ]);
        setSnapshot(snap);
        setDelivery(del);
      } catch (err) {
        notify(err instanceof Error ? err.message : '加载会话详情失败', 'error');
      }
    },
    [notify]
  );

  useEffect(() => {
    void loadMetaAndTasks();
  }, [loadMetaAndTasks]);

  useEffect(() => {
    if (viewMode === 'session' && activeTask) {
      void loadActiveSession(activeTask);
    }
  }, [viewMode, activeTask, loadActiveSession]);

  // Poll live tasks cleanly every 2.5s
  useEffect(() => {
    if (viewMode !== 'session' || !activeTask) return;
    const interval = snapshot?.worker_live ? 2000 : 6000;
    const id = setInterval(() => {
      void loadActiveSession(activeTask);
      void api.getTasks().then(setTasks).catch(() => {});
    }, interval);
    return () => clearInterval(id);
  }, [viewMode, activeTask, snapshot?.worker_live, loadActiveSession]);

  const handleSelectTask = (name: string) => {
    setActiveTask(name);
    setSelectedFile(null);
    setViewMode('session');
  };

  const handleCreated = async (taskName: string) => {
    await loadMetaAndTasks();
    setActiveTask(taskName);
    setSelectedFile(null);
    setViewMode('session');
    notify('研究会话已创建并开始运行');
  };

  return (
    <div className="relative flex h-full w-full overflow-hidden">
      {/* Left Sidebar */}
      {sidebarOpen ? (
        <Sidebar
          meta={meta}
          tasks={tasks}
          activeTask={activeTask}
          viewMode={viewMode}
          dark={dark}
          onToggleTheme={() => setDark((d) => !d)}
          onNewResearch={() => {
            setViewMode('new');
            setDrawerOpen(false);
          }}
          onSelectTask={handleSelectTask}
          onOpenSettings={() => {
            setViewMode('settings');
            setDrawerOpen(false);
          }}
          onRefresh={() => void loadMetaAndTasks()}
          onCollapse={() => setSidebarOpen(false)}
        />
      ) : (
        <div
          className="flex h-full w-11 shrink-0 flex-col items-center border-r py-3"
          style={{ backgroundColor: 'var(--bg-sidebar)', borderColor: 'var(--border-subtle)' }}
        >
          <button
            onClick={() => setSidebarOpen(true)}
            title="展开侧栏"
            className="rounded-lg p-1.5 opacity-75 hover:opacity-100 cursor-pointer"
          >
            <PanelLeftOpen className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* Center Content */}
      <main className="flex min-w-0 flex-1 overflow-hidden">
        <div className="min-w-0 flex-1 overflow-hidden">
          {viewMode === 'new' ? (
            <NewResearchView
              meta={meta}
              reuseOrigin={reuseOrigin}
              onClearReuse={() => setReuseOrigin(null)}
              onCreated={handleCreated}
              onOpenSettings={() => setViewMode('settings')}
              onNotify={notify}
            />
          ) : viewMode === 'settings' ? (
            <SettingsView onUpdated={loadMetaAndTasks} onNotify={notify} />
          ) : snapshot ? (
            <SessionView
              meta={meta}
              snapshot={snapshot}
              delivery={delivery}
              drawerOpen={drawerOpen}
              onToggleDrawer={() => setDrawerOpen((v) => !v)}
              onSelectArtifact={(file) => {
                setSelectedFile(file);
                setDrawerOpen(true);
              }}
              onReload={async () => {
                if (activeTask) await loadActiveSession(activeTask);
                await loadMetaAndTasks();
              }}
              onNotify={notify}
            />
          ) : (
            <div className="flex h-full items-center justify-center text-[13.5px]" style={{ color: 'var(--text-muted)' }}>
              正在加载会话…
            </div>
          )}
        </div>

        {/* Right Artifact & Evidence Drawer */}
        {viewMode === 'session' && drawerOpen && snapshot && (
          <ArtifactDrawer
            snapshot={snapshot}
            delivery={delivery}
            selectedFile={selectedFile}
            onSelectFile={setSelectedFile}
            onReuseVersion={(origin) => {
              setReuseOrigin(origin);
              setViewMode('new');
              setDrawerOpen(false);
            }}
            onClose={() => setDrawerOpen(false)}
          />
        )}
      </main>

      {/* Floating Toast */}
      {toast && (
        <div
          className={`fixed bottom-5 right-5 z-50 flex max-w-md items-center gap-2.5 rounded-xl border px-4 py-2.5 text-[13px] shadow-lg ${
            toast.tone === 'error'
              ? 'border-rose-500/40 bg-rose-950/90 text-rose-200'
              : 'border-blue-500/30 bg-slate-900/95 text-slate-100'
          }`}
        >
          <span>{toast.msg}</span>
          <button onClick={() => setToast(null)} className="opacity-70 hover:opacity-100 cursor-pointer">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
    </div>
  );
}
