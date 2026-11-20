import { useEffect, useState } from 'react';
import {
  CheckCircle2,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Server,
  ShieldCheck,
} from 'lucide-react';
import { api } from '../api';
import type { ModelSettingsProfile, ModelSettingsResponse } from '../types';

interface SettingsViewProps {
  onUpdated: () => Promise<void>;
  onNotify: (msg: string, tone?: 'info' | 'error') => void;
}

export function SettingsView({ onUpdated, onNotify }: SettingsViewProps) {
  const [settings, setSettings] = useState<ModelSettingsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [configPathDraft, setConfigPathDraft] = useState('');
  const [switchingPath, setSwitchingPath] = useState(false);

  const [selectedName, setSelectedName] = useState<string>('__new__');
  const [name, setName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [apiKeyEnv, setApiKeyEnv] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [isDefault, setIsDefault] = useState(false);

  const loadForm = (profile: ModelSettingsProfile | null) => {
    if (!profile) {
      setSelectedName('__new__');
      setName('');
      setBaseUrl('');
      setModel('');
      setApiKeyEnv('');
      setApiKey('');
      setIsDefault(false);
      return;
    }
    setSelectedName(profile.name);
    setName(profile.name);
    setBaseUrl(profile.base_url);
    setModel(profile.model);
    setApiKeyEnv(profile.api_key_env);
    setApiKey('');
    setIsDefault(profile.is_default);
  };

  const fetchSettings = async () => {
    try {
      setLoading(true);
      const data = await api.getModelSettings();
      setSettings(data);
      setConfigPathDraft(data.path);
      if (data.profiles.length > 0) {
        const target =
          data.profiles.find((p) => p.name === selectedName) ||
          data.profiles.find((p) => p.is_default) ||
          data.profiles[0];
        loadForm(target);
      } else {
        loadForm(null);
      }
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '读取模型设置失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void fetchSettings();
  }, []);

  const currentProfile = settings?.profiles.find((p) => p.name === selectedName) || null;

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settings) return;
    try {
      setSaving(true);
      const res = await api.saveModelProfile({
        catalog_sha256: settings.catalog_sha256,
        name: name.trim(),
        base_url: baseUrl.trim(),
        model: model.trim(),
        api_key_env: apiKeyEnv.trim(),
        api_key: apiKey.trim(),
        default: isDefault,
      });
      setSettings(res.settings);
      const saved = res.settings.profiles.find((p) => p.name === name.trim()) || null;
      loadForm(saved);
      await onUpdated();
      onNotify(`模型连接 “${name.trim()}” 已保存`);
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '保存失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  const credBadge = (src: ModelSettingsProfile['credential_source']) => {
    if (src === 'env') {
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-400">
          <ShieldCheck className="h-3 w-3" />
          环境变量
        </span>
      );
    }
    if (src === 'keyring') {
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-blue-500/30 bg-blue-500/10 px-2 py-0.5 text-[11px] text-blue-400">
          <KeyRound className="h-3 w-3" />
          Windows 凭据库
        </span>
      );
    }
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-400">
        未配置 Key
      </span>
    );
  };

  const handleSwitchConfigPath = async () => {
    try {
      setSwitchingPath(true);
      const res = await api.setModelsConfigPath(configPathDraft.trim());
      setSettings(res.settings);
      setConfigPathDraft(res.settings.path);
      if (res.settings.profiles.length > 0) {
        loadForm(res.settings.profiles.find((p) => p.is_default) || res.settings.profiles[0]);
      } else {
        loadForm(null);
      }
      await onUpdated();
      onNotify(`已切换配置文件至 ${res.settings.path}`);
    } catch (err) {
      onNotify(err instanceof Error ? err.message : '切换配置文件失败', 'error');
    } finally {
      setSwitchingPath(false);
    }
  };

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <header
        className="flex shrink-0 flex-col gap-2.5 border-b px-6 py-4"
        style={{ borderColor: 'var(--border-subtle)' }}
      >
        <div className="flex items-center justify-between">
          <h1 className="text-[17px] font-semibold">模型与连接设置</h1>
          <button
            onClick={() => void fetchSettings()}
            className="inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[12.5px] cursor-pointer"
            style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-surface)' }}
          >
            <RefreshCw className="h-3.5 w-3.5" />
            <span>刷新</span>
          </button>
        </div>
        <div className="flex items-center gap-2">
          <span className="shrink-0 text-[12px]" style={{ color: 'var(--text-muted)' }}>
            配置文件：
          </span>
          <input
            value={configPathDraft}
            onChange={(e) => setConfigPathDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                void handleSwitchConfigPath();
              }
            }}
            placeholder="输入 models.toml 路径（如项目内 .local/models.toml）"
            className="flex-1 rounded-lg border px-2.5 py-1 font-mono text-[12px] outline-none"
            style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-surface)' }}
          />
          <button
            onClick={() => void handleSwitchConfigPath()}
            disabled={switchingPath || configPathDraft.trim() === (settings?.path || '')}
            className="shrink-0 rounded-lg border px-3 py-1 text-[12px] font-medium disabled:opacity-40 cursor-pointer"
            style={{
              borderColor: 'var(--accent)',
              backgroundColor: 'var(--accent-soft)',
              color: 'var(--accent)',
            }}
          >
            {switchingPath ? '切换中…' : '切换路径'}
          </button>
        </div>
      </header>

      {loading ? (
        <div className="flex flex-1 items-center justify-center gap-2" style={{ color: 'var(--text-muted)' }}>
          <Loader2 className="h-4 w-4 animate-spin" />
          <span>读取配置中…</span>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 overflow-hidden">
          {/* Left Profile List */}
          <div
            className="w-[270px] shrink-0 overflow-y-auto border-r p-3 space-y-1.5"
            style={{ borderColor: 'var(--border-subtle)', backgroundColor: 'var(--bg-sidebar)' }}
          >
            <button
              onClick={() => loadForm(null)}
              className="flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed px-3 py-2 text-[13px] font-medium transition-colors cursor-pointer"
              style={{
                borderColor: selectedName === '__new__' ? 'var(--accent)' : 'var(--border-strong)',
                color: selectedName === '__new__' ? 'var(--accent)' : 'var(--text-secondary)',
                backgroundColor: selectedName === '__new__' ? 'var(--accent-soft)' : 'transparent',
              }}
            >
              <Plus className="h-4 w-4" />
              <span>新建模型连接</span>
            </button>

            {(settings?.profiles || []).map((p) => {
              const active = selectedName === p.name;
              return (
                <button
                  key={p.name}
                  onClick={() => loadForm(p)}
                  className="flex w-full flex-col gap-1 rounded-xl border p-3 text-left transition-colors cursor-pointer"
                  style={{
                    backgroundColor: active ? 'var(--bg-surface)' : 'transparent',
                    borderColor: active ? 'var(--accent)' : 'var(--border-subtle)',
                  }}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-semibold text-[13.5px]">{p.name}</span>
                    {p.is_default && (
                      <span
                        className="shrink-0 rounded px-1.5 py-0.5 text-[10.5px] font-medium"
                        style={{ backgroundColor: 'var(--accent-soft)', color: 'var(--accent)' }}
                      >
                        默认
                      </span>
                    )}
                  </div>
                  <div className="truncate font-mono text-[11.5px]" style={{ color: 'var(--text-muted)' }}>
                    {p.model}
                  </div>
                  <div className="mt-0.5">{credBadge(p.credential_source)}</div>
                </button>
              );
            })}
          </div>

          {/* Right Editor Form */}
          <div className="flex-1 overflow-y-auto p-6">
            <form onSubmit={handleSave} className="mx-auto max-w-[580px] space-y-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-[15px] font-semibold">
                  <Server className="h-4.5 w-4.5" style={{ color: 'var(--accent)' }} />
                  <span>{selectedName === '__new__' ? '新建模型连接' : `编辑连接：${selectedName}`}</span>
                </div>
                {currentProfile && credBadge(currentProfile.credential_source)}
              </div>

              <div className="grid grid-cols-1 gap-4">
                <label className="flex flex-col gap-1.5">
                  <span className="text-[12.5px] font-medium">连接名称</span>
                  <input
                    required
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="例如：blsc-flash 或 deepseek"
                    className="rounded-xl border px-3.5 py-2 text-[13.5px] outline-none"
                    style={{ backgroundColor: 'var(--bg-surface)', borderColor: 'var(--border-strong)' }}
                  />
                </label>

                <label className="flex flex-col gap-1.5">
                  <span className="text-[12.5px] font-medium">Base URL（OpenAI 兼容接口）</span>
                  <input
                    required
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder="https://llmapi.blsc.cn/v1"
                    className="rounded-xl border px-3.5 py-2 font-mono text-[13px] outline-none"
                    style={{ backgroundColor: 'var(--bg-surface)', borderColor: 'var(--border-strong)' }}
                  />
                </label>

                <label className="flex flex-col gap-1.5">
                  <span className="text-[12.5px] font-medium">模型 ID</span>
                  <input
                    required
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    placeholder="DeepSeek-V4.1-Flash-1017"
                    className="rounded-xl border px-3.5 py-2 font-mono text-[13px] outline-none"
                    style={{ backgroundColor: 'var(--bg-surface)', borderColor: 'var(--border-strong)' }}
                  />
                </label>

                <label className="flex flex-col gap-1.5">
                  <span className="text-[12.5px] font-medium">凭据环境变量名（可选）</span>
                  <input
                    value={apiKeyEnv}
                    onChange={(e) => setApiKeyEnv(e.target.value)}
                    placeholder="例如：BLSC_API_KEY（留空则自动生成）"
                    className="rounded-xl border px-3.5 py-2 font-mono text-[13px] outline-none"
                    style={{ backgroundColor: 'var(--bg-surface)', borderColor: 'var(--border-strong)' }}
                  />
                </label>

                <label className="flex flex-col gap-1.5">
                  <span className="text-[12.5px] font-medium">API Key（安全存入 Windows 凭据管理器，不写明文文件）</span>
                  <input
                    type="password"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder={
                      currentProfile?.credential_source === 'env'
                        ? '当前已由系统环境变量提供（无需在此重复填写）'
                        : currentProfile?.credential_source === 'keyring'
                        ? '已保存至 Windows 凭据库，留空表示保持不变'
                        : '输入 API Key...'
                    }
                    className="rounded-xl border px-3.5 py-2 font-mono text-[13px] outline-none"
                    style={{ backgroundColor: 'var(--bg-surface)', borderColor: 'var(--border-strong)' }}
                  />
                </label>

                <label className="inline-flex items-center gap-2.5 pt-1 text-[13px] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={isDefault}
                    onChange={(e) => setIsDefault(e.target.checked)}
                    className="h-4 w-4 rounded"
                  />
                  <span>设为新建研究任务的默认文本模型</span>
                </label>
              </div>

              <div className="pt-2 flex items-center justify-between">
                <div className="flex items-center gap-1.5 text-[12px]" style={{ color: 'var(--text-muted)' }}>
                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                  <span>凭据按 Base URL 隔离，不会泄露到会话日志或仓库中</span>
                </div>
                <button
                  type="submit"
                  disabled={saving}
                  className="inline-flex items-center gap-1.5 rounded-xl px-4 py-2 text-[13px] font-medium text-white cursor-pointer disabled:opacity-50"
                  style={{ backgroundColor: 'var(--accent)' }}
                >
                  {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                  <span>保存连接</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
