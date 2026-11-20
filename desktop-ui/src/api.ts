import type {
  CreationPreview,
  DeliverySnapshot,
  FileEntry,
  MetaResponse,
  ModelProfileSummary,
  ModelSettingsResponse,
  TaskListItem,
  TaskSnapshot,
} from './types';

function readToken(): string {
  const hash = window.location.hash.replace(/^#/, '');
  if (hash) {
    sessionStorage.setItem('sar_token', hash);
    return hash;
  }
  return sessionStorage.getItem('sar_token') || '';
}

const TOKEN = readToken();

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return {
    Authorization: `Bearer ${TOKEN}`,
    ...extra,
  };
}

async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: authHeaders() });
  if (!res.ok) {
    let msg = `请求失败 (${res.status})`;
    try {
      const body = await res.json();
      if (body?.error) msg = body.error;
    } catch {
      // ignore
    }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

async function apiPost<T>(payload: Record<string, unknown>): Promise<T> {
  const res = await fetch('/api/action', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error || `操作失败 (${res.status})`);
  }
  return body as T;
}

export const api = {
  getMeta: () => apiGet<MetaResponse>('/api/meta'),
  getTasks: () => apiGet<TaskListItem[]>('/api/tasks'),
  getTask: (task: string) => apiGet<TaskSnapshot>(`/api/task?task=${encodeURIComponent(task)}`),
  getDelivery: (task: string) => apiGet<DeliverySnapshot>(`/api/delivery?task=${encodeURIComponent(task)}`),
  getFiles: (task: string, mount: string, path = '') =>
    apiGet<FileEntry[]>(
      `/api/files?task=${encodeURIComponent(task)}&mount=${encodeURIComponent(mount)}&path=${encodeURIComponent(path)}`
    ),
  getModelSettings: () => apiGet<ModelSettingsResponse>('/api/model-settings'),

  async fetchFileBlob(task: string, mount: string, path: string): Promise<{ blob: Blob; contentType: string }> {
    const url = `/api/file?task=${encodeURIComponent(task)}&mount=${encodeURIComponent(mount)}&path=${encodeURIComponent(path)}`;
    const res = await fetch(url, { headers: authHeaders() });
    if (!res.ok) {
      throw new Error('无法读取该文件');
    }
    return {
      blob: await res.blob(),
      contentType: res.headers.get('Content-Type') || 'application/octet-stream',
    };
  },

  async fetchPdfPage(task: string, mount: string, path: string, page: number): Promise<{ url: string; totalPages: number }> {
    const url = `/api/pdf-page?task=${encodeURIComponent(task)}&mount=${encodeURIComponent(mount)}&path=${encodeURIComponent(path)}&page=${page}`;
    const res = await fetch(url, { headers: authHeaders() });
    if (!res.ok) {
      throw new Error('无法渲染 PDF 页面（请检查 Poppler 环境）');
    }
    const totalPages = parseInt(res.headers.get('X-PDF-Pages') || '1', 10) || 1;
    const blob = await res.blob();
    return { url: URL.createObjectURL(blob), totalPages };
  },

  previewCreate: (payload: Record<string, unknown>) =>
    apiPost<CreationPreview>({ op: 'preview_create', ...payload }),

  createTask: (payload: Record<string, unknown>) =>
    apiPost<{ task: string }>({ op: 'create', confirmed: true, ...payload }),

  sendFeedback: (task: string, feedback: string, independentReview: boolean) =>
    apiPost<{ task: string; message: string }>({
      op: 'feedback',
      task,
      submission_id: crypto.randomUUID(),
      feedback,
      independent_review_required: independentReview,
    }),

  stopTask: (task: string) => apiPost<{ message: string }>({ op: 'stop', task }),
  resumeTask: (task: string) => apiPost<{ task: string; message: string }>({ op: 'resume', task }),

  approveExecution: (task: string, token: string, remember: boolean) =>
    apiPost<{ task: string; message: string }>({
      op: 'approve',
      task,
      token,
      remember_execution: remember,
    }),

  rejectExecution: (task: string, token: string, reason: string) =>
    apiPost<{ task: string; message: string }>({
      op: 'reject',
      task,
      token,
      reason: reason || '用户拒绝执行该命令',
    }),

  switchModel: (task: string, model: string, bindingId: string, reason: string) =>
    apiPost<{ task: string }>({
      op: 'switch',
      task,
      model,
      binding_id: bindingId,
      switch_id: crypto.randomUUID(),
      reason: reason || '用户在桌面工作台切换模型',
      confirmed: true,
    }),

  saveModelProfile: (payload: {
    catalog_sha256: string;
    name: string;
    base_url: string;
    model: string;
    api_key_env: string;
    api_key: string;
    default: boolean;
  }) =>
    apiPost<{ settings: ModelSettingsResponse; profiles: ModelProfileSummary[] }>({
      op: 'save_model_profile',
      ...payload,
    }),

  setModelsConfigPath: (path: string) =>
    apiPost<{ settings: ModelSettingsResponse; profiles: ModelProfileSummary[] }>({
      op: 'set_models_config_path',
      path,
    }),
};

export const desktopBridge = {
  isNative: () => Boolean(window.pywebview?.api),
  async pickFiles(): Promise<string[]> {
    if (window.pywebview?.api?.pick_files) {
      return (await window.pywebview.api.pick_files()) || [];
    }
    return [];
  },
  async pickFolder(): Promise<string[]> {
    if (window.pywebview?.api?.pick_folder) {
      return (await window.pywebview.api.pick_folder()) || [];
    }
    return [];
  },
};
