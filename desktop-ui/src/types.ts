export interface ModelProfileSummary {
  selector: string;
  model: string;
  default: boolean;
  binding_id: string;
  recipients: Record<string, { base_url?: string; model?: string; api?: string } | null>;
  input_price?: number | null;
  output_price?: number | null;
}

export interface CreationDefaults {
  max_turns: number;
  max_tokens: number;
  max_images: number;
  allow_execution: boolean;
  sandbox_execution: boolean;
  allow_network: boolean;
  allow_vision: boolean;
  independent_review_required: boolean;
  process_invocations: number | null;
  process_wall_seconds: number | null;
}

export interface MetaResponse {
  project: string;
  reads: string[];
  tasks: string;
  profiles: ModelProfileSummary[];
  creation_defaults: CreationDefaults;
}

export interface TaskListItem {
  task: string;
  topic: string;
  status: string;
}

export interface ActivityItem {
  attempt?: string;
  tool?: string;
  status?: string;
  summary?: string;
  error?: string;
  arguments?: unknown;
  observed?: unknown;
}

export interface PendingApproval {
  token: string;
  attempt: string;
  call?: {
    id?: string;
    function?: { name?: string; arguments?: string };
  };
  execution?: {
    argv: string[];
    cwd: string;
    timeout_sec: number;
  };
}

export interface ResourceEntry {
  reservation_id: string;
  status: string;
  reserved?: Record<string, number>;
  actual?: Record<string, number>;
  purpose?: string;
  attempt_id?: string;
  actual_source?: string;
  reason?: string;
}

export interface TaskSnapshot {
  task: string;
  mode: string;
  worker_live: boolean;
  stop_requested: boolean;
  worker_error: string | null;
  local_workers: string[];
  manifest: {
    session_id?: string;
    topic?: string;
    status: string;
    status_reason?: string;
    current_attempt?: string;
    revision?: number;
    updated_at?: string;
    budget?: {
      attempts?: number;
      max_attempts?: number;
      no_progress?: number;
      max_no_progress?: number;
    };
  };
  requests: string[];
  plan?: unknown;
  pending?: unknown;
  feedback_submissions?: Array<{
    submission_id?: string;
    status?: string;
    feedback?: string;
    independent_review_required?: boolean;
  }>;
  approval: PendingApproval | null;
  approval_unavailable: string | null;
  activity: ActivityItem[];
  activity_total: number;
  queued_actions: number;
  model: string | null;
  saved_connections: Record<string, { base_url?: string; model?: string; api?: string } | null>;
  access: {
    allow_network?: boolean;
    allow_execution?: boolean;
    sandbox_execution?: boolean;
    allow_vision?: boolean;
  };
  sources: string[];
  source_access: Record<string, boolean>;
  resources: {
    limits?: Record<string, number | null>;
    used?: Record<string, number>;
    reserved?: Record<string, number>;
    remaining?: Record<string, number | null>;
    unknown_reservations?: number;
    entries?: ResourceEntry[];
  };
}

export interface DeliveryFile {
  group: string;
  mount: string;
  path: string;
  name: string;
}

export interface DeliveryVersion {
  number: number;
  status: string;
  summary: string;
  file_count: number;
  origin: {
    from_session: string;
    reuse_version: number;
    reuse: string[];
  };
}

export interface DeliverySnapshot {
  label: string;
  current: boolean;
  summary?: string | null;
  unresolved: string[];
  limitations: string[];
  files: DeliveryFile[];
  unavailable_file_count?: number;
  versions?: DeliveryVersion[];
  review?: unknown;
  review_notice?: string;
  diagnostic?: string;
}

export interface FileEntry {
  name: string;
  path: string;
  directory: boolean;
}

export interface CreationPreview {
  preview_id: string;
  goal: string;
  task_root: string;
  model: string;
  binding_id: string;
  recipients: Record<string, { base_url?: string; model?: string; api?: string } | null>;
  sources: Record<string, string>;
  access: Record<string, boolean>;
  independent_review_required: boolean;
  limits: Record<string, number | null>;
  attempts?: { max_attempts?: number; max_no_progress?: number };
}

export interface ModelSettingsProfile {
  name: string;
  api: string;
  base_url: string;
  model: string;
  api_key_env: string;
  capabilities: string[];
  credential_source: 'env' | 'keyring' | 'missing';
  is_default: boolean;
}

export interface ModelSettingsResponse {
  path: string;
  catalog_sha256: string;
  default_text: string | null;
  profiles: ModelSettingsProfile[];
}

declare global {
  interface Window {
    pywebview?: {
      api?: {
        pick_files?: () => Promise<string[]>;
        pick_folder?: () => Promise<string[]>;
      };
    };
  }
}
