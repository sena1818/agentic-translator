/**
 * API 客户端
 * 封装所有后端 API 调用
 */
import axios from "axios";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 60000,
  headers: {
    "Content-Type": "application/json",
  },
});

// ============== 类型定义 ==============

export type { TaskProgress, TranslationTask, TaskStatus } from "./generated";
import type { TranslationTask, TaskStatus } from "./generated";

export interface TaskListResponse {
  tasks: TranslationTask[];
  total: number;
  stats: Partial<Record<TaskStatus, number>>;
}

export interface Glossary {
  id: string;
  name: string;
  term_count?: number;
  terms?: Record<string, string>;
  updated_at?: string;
}

export interface BilingualParagraph {
  source: string;
  translation: string;
}

export interface BilingualPreview {
  task_id: string;
  filename: string;
  bilingual: boolean;
  count: number;
  paragraphs: BilingualParagraph[];
}

// ============== 翻译任务 API ==============

/**
 * 创建翻译任务
 */
export const createTranslationTask = async (
  file: File,
  glossaryId?: string,
  bilingual: boolean = false,
  book: { author?: string; domain?: string } = {},
): Promise<TranslationTask> => {
  const formData = new FormData();
  formData.append("file", file);
  if (glossaryId) {
    formData.append("glossary_id", glossaryId);
  }
  formData.append("bilingual", String(bilingual));
  if (book.author?.trim()) formData.append("author", book.author.trim());
  if (book.domain?.trim()) formData.append("domain", book.domain.trim());

  const response = await apiClient.post<TranslationTask>(
    "/api/translation/tasks",
    formData,
    {
      headers: {
        "Content-Type": "multipart/form-data",
      },
    },
  );

  return response.data;
};

/**
 * 查询任务状态
 */
export const getTaskStatus = async (
  taskId: string,
): Promise<TranslationTask> => {
  const response = await apiClient.get<TranslationTask>(
    `/api/translation/tasks/${taskId}`,
  );
  return response.data;
};

/**
 * 获取任务列表
 */
export const getTaskList = async (
  skip = 0,
  limit = 20,
  search = "",
  status = "",
): Promise<TaskListResponse> => {
  const response = await apiClient.get<TaskListResponse>(
    "/api/translation/tasks",
    {
      params: { skip, limit, search, status: status || undefined },
    },
  );
  return response.data;
};

/**
 * 删除任务
 */
export const deleteTask = async (taskId: string): Promise<void> => {
  await apiClient.delete(`/api/translation/tasks/${taskId}`);
};

/**
 * 下载翻译结果
 * @param taskId 任务ID
 * @param format 导出格式: 'md' | 'html' | 'epub' | 'zip'
 * @param variant 结果类型: 'mono' | 'bilingual'
 */
export const downloadResult = (
  taskId: string,
  format: "md" | "html" | "epub" | "zip" = "md",
  variant: "mono" | "bilingual" = "mono",
): string => {
  return `${API_BASE_URL}/api/files/results/${taskId}?format=${format}&variant=${variant}`;
};

/** 结构化导出的双栏对照阅读器：在浏览器内打开（段级双向跳转、目录、脚注浮层）。 */
export const readerUrl = (taskId: string): string =>
  `${API_BASE_URL}/api/files/results/${taskId}?format=html&inline=true`;

/** 应用内阅读页（Hash 路由，可收藏、刷新后仍停在该页）；seg 为要定位的全书段编号。 */
export const readerPagePath = (taskId: string, seg?: number): string =>
  `/tasks/${taskId}/read${seg === undefined ? "" : `?seg=${seg}`}`;

export interface StructureStats {
  task_id: string;
  segments: number;
  dead_links: string[];
  missing_images: string[];
  epub: string[];
}

export interface MeaningHit {
  position: number;
  seg_id: number | null;
  section_path: string[];
  source: string;
  translation: string | null;
  score: number;
  lexical_rank: number | null;
  vector_rank: number | null;
  similarity: number | null;
}

export interface MeaningSearchResult {
  query: string;
  /** hybrid：关键词 + 向量；lexical：仅关键词（hint 说明原因） */
  mode: "hybrid" | "lexical";
  model: string | null;
  hint: string;
  items: MeaningHit[];
}

/** 按意思检索全书：中英文查询，原文与译文都参与匹配。 */
export const searchBookByMeaning = async (
  taskId: string,
  q: string,
  limit = 10,
): Promise<MeaningSearchResult> =>
  (
    await apiClient.get<MeaningSearchResult>(
      `/api/translation/tasks/${taskId}/index/semantic`,
      { params: { q, limit } },
    )
  ).data;

/** 结构化导出统计（缺失图片清单等）；历史任务没有，返回 404。 */
export const getStructure = async (taskId: string): Promise<StructureStats> => {
  const response = await apiClient.get<StructureStats>(
    `/api/files/results/${taskId}/structure`,
  );
  return response.data;
};

/**
 * 获取双语对照预览（Web 界面内双栏渲染，无需下载）
 */
export const getBilingualPreview = async (
  taskId: string,
): Promise<BilingualPreview> => {
  const response = await apiClient.get<BilingualPreview>(
    `/api/files/results/${taskId}/preview`,
  );
  return response.data;
};

// ============== 术语表 API ==============

/**
 * 获取术语表列表
 */
export const getGlossaryList = async (): Promise<Glossary[]> => {
  const response = await apiClient.get<Glossary[]>("/api/glossary/");
  return response.data;
};

/**
 * 获取术语表详情
 */
export const getGlossary = async (glossaryId: string): Promise<Glossary> => {
  const response = await apiClient.get<Glossary>(`/api/glossary/${glossaryId}`);
  return response.data;
};

/**
 * 创建术语表
 */
export const createGlossary = async (
  name: string,
  terms: Record<string, string>,
): Promise<Glossary> => {
  const response = await apiClient.post<Glossary>("/api/glossary/", {
    name,
    terms,
  });
  return response.data;
};

/**
 * 更新术语表(全量)
 */
export const updateGlossary = async (
  glossaryId: string,
  terms: Record<string, string>,
): Promise<void> => {
  await apiClient.put(`/api/glossary/${glossaryId}`, { terms });
};

/**
 * 增量修改术语
 */
export const modifyGlossaryTerms = async (
  glossaryId: string,
  add?: Record<string, string>,
  remove?: string[],
): Promise<number> => {
  const response = await apiClient.patch<{ term_count: number }>(
    `/api/glossary/${glossaryId}/terms`,
    { add, remove },
  );
  return response.data.term_count;
};

/**
 * 删除术语表
 */
export const deleteGlossary = async (glossaryId: string): Promise<void> => {
  await apiClient.delete(`/api/glossary/${glossaryId}`);
};

/**
 * 导入术语表
 */
export const importGlossary = async (
  file: File,
  name: string,
): Promise<Glossary> => {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("name", name);

  const response = await apiClient.post<Glossary>(
    "/api/glossary/import",
    formData,
    {
      headers: {
        "Content-Type": "multipart/form-data",
      },
    },
  );

  return response.data;
};

/**
 * 导出术语表
 */
export const exportGlossary = (glossaryId: string): string => {
  return `${API_BASE_URL}/api/glossary/${glossaryId}/export`;
};

export interface SystemState {
  workers_online: number;
  formats: string[];
  max_upload_bytes: number;
}
export interface ChunkResult {
  chunk_index: number;
  chunk_id: string;
  original: string;
  translation: string;
  success: boolean;
  cached: boolean;
  repaired: boolean;
  quality_report: {
    passed?: boolean;
    /** 人工编辑过（保存过译文） */
    edited?: boolean;
    /** 已审校（显式标记，与编辑过分开） */
    reviewed?: boolean;
    /** 由书内传播改过的术语 */
    propagated?: string;
    issues?: Array<{
      kind: string;
      severity: string;
      message: string;
      /** 结构化模式下的全书段编号，可在对照阅读器中定位 */
      segment?: number;
    }>;
  };
}
export interface ChunkPage {
  chunks: ChunkResult[];
  total: number;
  revision: string | null;
}
export const getSystemState = async (): Promise<SystemState> =>
  (await apiClient.get("/api/system")).data;
export const cancelTranslation = async (id: string) => {
  await apiClient.post(`/api/translation/tasks/${id}/cancel`);
};
export const retryTranslation = async (id: string, indices?: number[]) => {
  await apiClient.post(`/api/translation/tasks/${id}/retry`, {
    indices: indices ?? null,
  });
};
export const getChunks = async (id: string, skip = 0): Promise<ChunkPage> =>
  (
    await apiClient.get(`/api/translation/tasks/${id}/chunks`, {
      params: { skip, limit: 10 },
    })
  ).data;
/** 人工编辑里识别出的译名纠正（改掉的正是某个术语的译名） */
export interface TermChange {
  term: string;
  old_rendering: string;
  new_rendering: string;
  count: number;
}
export const editChunk = async (
  id: string,
  index: number,
  translation: string,
  revision: string | null,
): Promise<ChunkPage & { term_changes?: TermChange[] }> =>
  (
    await apiClient.put(`/api/translation/tasks/${id}/chunks/${index}`, {
      translation,
      revision,
    })
  ).data;
export const markReviewed = async (
  id: string,
  index: number,
  revision: string | null,
  reviewed = true,
) => {
  await apiClient.post(`/api/translation/tasks/${id}/chunks/${index}/review`, {
    revision,
    reviewed,
  });
};
export interface PropagationItem {
  position: number;
  seg_id: number | null;
  chunk_index: number;
  source: string;
  translation: string;
  preview: string;
  count: number;
}
export interface PropagationPreview {
  term: string;
  old: string;
  new: string;
  total_segments: number;
  total_replacements: number;
  items: PropagationItem[];
  skipped: Array<{
    position: number;
    seg_id: number | null;
    chunk_index: number;
    reason: string;
  }>;
  revision: string | null;
}
export interface TermCorrection {
  term: string;
  old: string;
  new: string;
}
export const previewTermCorrection = async (
  id: string,
  correction: TermCorrection,
): Promise<PropagationPreview> =>
  (
    await apiClient.post(
      `/api/translation/tasks/${id}/corrections/preview`,
      correction,
    )
  ).data;
export const applyTermCorrection = async (
  id: string,
  correction: TermCorrection,
  positions: number[],
  revision: string | null,
): Promise<{ replacements: number; applied_segments: number[] }> =>
  (
    await apiClient.post(`/api/translation/tasks/${id}/corrections/apply`, {
      ...correction,
      positions,
      revision,
    })
  ).data;
export interface TermSuggestion {
  term: string;
  rendering: string;
  book: string;
  scope: string;
  source: string;
  label: string;
}
export const getTermSuggestions = async (
  id: string,
): Promise<{ items: TermSuggestion[] }> =>
  (await apiClient.get(`/api/translation/tasks/${id}/term-suggestions`)).data;
export interface TermReview {
  task_id: string;
  book: string;
  term: string;
  reason: string;
  contexts: string[];
  evidence: { tool: string; excerpt: string }[];
  steps: number;
}
export const getTermReviews = async (): Promise<TermReview[]> =>
  (await apiClient.get("/api/translation/term-reviews")).data;
export const errorText = (error: unknown): string =>
  axios.isAxiosError(error)
    ? typeof error.response?.data?.detail === "string"
      ? error.response.data.detail
      : error.message
    : error instanceof Error
      ? error.message
      : "请求失败，请重试";
export const assetUrl = (id: string, src: string) =>
  src.startsWith(`assets/${id}/`)
    ? `${API_BASE_URL}/api/files/results/${id}/assets/${encodeURIComponent(src.split("/").pop()!)}`
    : src;
export interface GlossaryCandidate {
  term: string;
  translation: string;
  note: string;
}
export const suggestGlossary = async (
  file: File,
  glossaryId?: string,
): Promise<GlossaryCandidate[]> => {
  const form = new FormData();
  form.append("file", file);
  if (glossaryId) form.append("glossary_id", glossaryId);
  return (
    await apiClient.post("/api/glossary/draft", form, {
      timeout: 180000,
      headers: { "Content-Type": "multipart/form-data" },
    })
  ).data.candidates;
};
