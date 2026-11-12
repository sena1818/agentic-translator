import { useState } from "react";
import { App, Pagination } from "antd";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import BookSearch from "./BookSearch";
import ReviewAgentPanel from "./ReviewAgentPanel";
import remarkGfm from "remark-gfm";
import {
  getTaskStatus,
  getChunks,
  cancelTranslation,
  retryTranslation,
  editChunk,
  markReviewed,
  downloadResult,
  readerUrl,
  readerPagePath,
  getStructure,
  errorText,
  assetUrl,
  type ChunkResult,
  type TermCorrection,
} from "../services/api";
import { TermCorrectionPanel, TermSuggestions } from "./TermCorrectionPanel";
import {
  statusLabels,
  chipClass,
  stageLabels,
  toCommonMark,
} from "../services/taskPresentation";

const running = (status: string) => ["pending", "processing"].includes(status);
function Markdown({ text, id }: { text: string; id: string }) {
  return (
    <div className="rendered-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          img: ({ src, alt }) => (
            <img src={assetUrl(id, src || "")} alt={alt || ""} loading="lazy" />
          ),
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {toCommonMark(text)}
      </ReactMarkdown>
    </div>
  );
}

function ChunkEditor({
  chunk,
  id,
  revision,
  onSaved,
  onTermChange,
}: {
  chunk: ChunkResult;
  id: string;
  revision: string | null;
  onSaved: () => void;
  onTermChange?: (change: TermCorrection) => void;
}) {
  const { message } = App.useApp();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(chunk.translation);
  const save = useMutation({
    mutationFn: () => editChunk(id, chunk.chunk_index, draft, revision),
    onSuccess: (data) => {
      setEditing(false);
      onSaved();
      const change = data?.term_changes?.[0];
      if (change && onTermChange) {
        onTermChange({
          term: change.term,
          old: change.old_rendering,
          new: change.new_rendering,
        });
        message.info(
          `检测到译名修改：${change.term} ${change.old_rendering} → ${change.new_rendering}，可在"全书改译名"中预览其余出现处`,
        );
      } else {
        message.success("修改已保存，下载结果已更新");
      }
    },
    onError: (e) => message.error(errorText(e)),
  });
  const review = useMutation({
    mutationFn: () => markReviewed(id, chunk.chunk_index, revision),
    onSuccess: onSaved,
    onError: (e) => message.error(errorText(e)),
  });
  const retry = useMutation({
    mutationFn: () => retryTranslation(id, [chunk.chunk_index]),
    onSuccess: onSaved,
    onError: (e) => message.error(errorText(e)),
  });
  return (
    <article className="review-chunk" id={`chunk-${chunk.chunk_index}`}>
      <header className="chunk-heading">
        <span>块 {chunk.chunk_index + 1}</span>
        <span
          className={`chip ${!chunk.success ? "chip-fail" : chunk.quality_report.passed === false ? "chip-warn" : "chip-done"}`}
        >
          {!chunk.success
            ? "翻译失败"
            : chunk.quality_report.reviewed
              ? "已审校"
              : chunk.quality_report.passed === false
                ? "待复核"
                : "质检通过"}
        </span>
        {chunk.quality_report.edited && (
          <span className="chip chip-edited">人工编辑过</span>
        )}
        <div className="td-actions">
          {chunk.success && !chunk.quality_report.reviewed && (
            <button
              className="btn-outline"
              disabled={review.isPending || save.isPending}
              onClick={() => review.mutate()}
            >
              标记已审校
            </button>
          )}
          <button
            className="btn-outline"
            disabled={retry.isPending || save.isPending}
            onClick={() => {
              setDraft(chunk.translation);
              setEditing(true);
            }}
          >
            编辑译文
          </button>
          <button
            className="btn-outline"
            disabled={retry.isPending || save.isPending}
            onClick={() => retry.mutate()}
          >
            重译此块
          </button>
        </div>
      </header>
      {!!chunk.quality_report.issues?.length && (
        <ul className="quality-issues">
          {chunk.quality_report.issues.map((issue, i) => (
            <li key={i}>
              {issue.message}
              {issue.segment !== undefined && (
                <>
                  {" "}
                  <Link
                    className="issue-locate"
                    to={readerPagePath(id, issue.segment)}
                  >
                    在对照阅读器中定位
                  </Link>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="review-columns">
        <section>
          <h3>原文</h3>
          <Markdown text={chunk.original} id={id} />
        </section>
        <section>
          <h3>译文</h3>
          {editing ? (
            <>
              <textarea
                className="translation-editor"
                aria-label={`块 ${chunk.chunk_index + 1} 译文`}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={12}
              />
              <div className="td-actions">
                <button
                  className="btn-accent"
                  disabled={!draft.trim() || save.isPending}
                  onClick={() => save.mutate()}
                >
                  保存审校
                </button>
                <button
                  className="btn-outline"
                  onClick={() => setEditing(false)}
                >
                  放弃修改
                </button>
              </div>
            </>
          ) : (
            <Markdown text={chunk.translation} id={id} />
          )}
        </section>
      </div>
    </article>
  );
}

export default function TaskDetail() {
  const { taskId: id = "" } = useParams();
  const { message } = App.useApp();
  const client = useQueryClient();
  const [page, setPage] = useState(1);
  const [reviewOnly, setReviewOnly] = useState(false);
  const [correction, setCorrection] = useState<TermCorrection | null>(null);
  const task = useQuery({
    queryKey: ["task", id],
    queryFn: () => getTaskStatus(id),
    refetchInterval: (query) =>
      running(query.state.data?.status || "pending") ? 2000 : false,
  });
  const chunks = useQuery({
    queryKey: ["chunks", id, page, task.data?.updated_at],
    queryFn: () => getChunks(id, (page - 1) * 10),
    enabled: !!task.data && !running(task.data.status),
  });
  const missingImages = task.data?.quality_summary?.missing_images ?? 0;
  const structure = useQuery({
    queryKey: ["structure", id, task.data?.updated_at],
    queryFn: () => getStructure(id),
    enabled: missingImages > 0,
  });
  const refresh = () => {
    client.invalidateQueries({ queryKey: ["task", id] });
    client.invalidateQueries({ queryKey: ["chunks", id] });
    client.invalidateQueries({ queryKey: ["tasks"] });
  };
  const cancel = useMutation({
    mutationFn: () => cancelTranslation(id),
    onSuccess: refresh,
    onError: (e) => message.error(errorText(e)),
  });
  const retry = useMutation({
    mutationFn: () => retryTranslation(id),
    onSuccess: refresh,
    onError: (e) => message.error(errorText(e)),
  });
  if (task.isError)
    return (
      <main className="page">
        <Link to="/">← 任务列表</Link>
        <div className="alert-banner error" role="alert">
          无法读取任务：{errorText(task.error)}
          <button className="btn-outline" onClick={() => task.refetch()}>
            重试
          </button>
        </div>
      </main>
    );
  if (!task.data)
    return (
      <main className="page" role="status">
        正在读取任务…
      </main>
    );
  const t = task.data;
  const active = running(t.status);
  const downloadable = !!t.result_url && !active;
  const structured = !!t.quality_summary?.structured;
  const stages = [
    "queued",
    "converting",
    "analyzing",
    "translating",
    "exporting",
    "finished",
  ];
  const visible =
    chunks.data?.chunks.filter(
      (c) => !reviewOnly || !c.success || c.quality_report.passed === false,
    ) || [];
  return (
    <main className="page task-detail">
      <Link className="back-link" to="/">
        ← 任务列表
      </Link>
      <div className="workspace-heading">
        <div>
          <h1>{t.filename}</h1>
          <p>
            <span className={chipClass(t.status)}>
              {statusLabels[t.status]}
            </span>{" "}
            · {t.bilingual ? "双语对照" : "中文译文"}
          </p>
        </div>
        <div className="td-actions">
          {active ? (
            <button
              className="btn-outline"
              disabled={cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              取消任务
            </button>
          ) : (
            (t.status === "cancelled" ||
              t.status === "failed" ||
              t.quality_summary.failed > 0 ||
              !t.quality_summary.total) && (
              <button
                className="btn-accent"
                disabled={retry.isPending}
                onClick={() => retry.mutate()}
              >
                {t.status === "cancelled" ? "恢复任务" : "重试失败块"}
              </button>
            )
          )}
        </div>
      </div>
      <ol className="task-stages">
        {stages.map((stage, i) => (
          <li
            key={stage}
            className={stages.indexOf(t.stage) >= i ? "reached" : ""}
          >
            {stageLabels[stage]}
          </li>
        ))}
      </ol>
      <section className="task-progress">
        <progress max={100} value={t.progress.percentage} />
        <p>
          {t.progress.current} / {t.progress.total} 块 ·{" "}
          {t.progress.percentage.toFixed(1)}% · {t.progress.speed.toFixed(1)}{" "}
          块/分钟
        </p>
      </section>
      {t.error && (
        <div
          className={`alert-banner ${t.status === "failed" ? "error" : "warn"}`}
          role="status"
        >
          {t.error}
        </div>
      )}
      {!!t.quality_summary.total && (
        <p className="quality-summary">
          {t.quality_summary.total} 块 · {t.quality_summary.failed} 个失败 ·{" "}
          {t.quality_summary.needs_review} 个待复核 ·{" "}
          {t.quality_summary.repaired} 个已修复
          {structured && (
            <>
              {" "}
              · {t.quality_summary.segments} 段对齐 ·{" "}
              {t.quality_summary.dead_links ?? 0} 个失效内链
            </>
          )}
        </p>
      )}
      {!!t.quality_summary?.ledger_terms && (
        <p className="ledger-summary">
          术语账本 {t.quality_summary.ledger_terms} 条
          {!!t.quality_summary.ledger_terms_added &&
            `（本次新增 ${t.quality_summary.ledger_terms_added} 条）`}
          ：自动统一全书人名与专名译法，已保存为术语表，审阅可选。{" "}
          <Link to="/glossary">去术语表审阅 →</Link>
        </p>
      )}
      {downloadable && (
        <section className="download-actions" aria-label="下载结果">
          {structured && (
            <>
              <Link className="btn-dl-html" to={readerPagePath(id)}>
                对照阅读
              </Link>
              <a
                className="btn-outline"
                href={readerUrl(id)}
                target="_blank"
                rel="noopener noreferrer"
              >
                新窗口打开阅读器
              </a>
            </>
          )}
          <a className="btn-dl-md" href={downloadResult(id)}>
            中文 Markdown
          </a>
          {t.bilingual && (
            <>
              <a
                className="btn-dl-md"
                href={downloadResult(id, "md", "bilingual")}
              >
                双语 Markdown
              </a>
              {!structured && (
                <a
                  className="btn-dl-html"
                  href={downloadResult(id, "html", "bilingual")}
                >
                  双栏 HTML
                </a>
              )}
            </>
          )}
          {(t.quality_summary.epub ?? 0) >= 1 && (
            <a className="btn-dl-md" href={downloadResult(id, "epub")}>
              中文 EPUB
            </a>
          )}
          {(t.quality_summary.epub ?? 0) >= 2 && (
            <a
              className="btn-dl-md"
              href={downloadResult(id, "epub", "bilingual")}
            >
              双语 EPUB
            </a>
          )}
          <a className="btn-outline" href={downloadResult(id, "zip")}>
            图片资源 ZIP
          </a>
        </section>
      )}
      {downloadable && missingImages > 0 && (
        <div className="alert-banner warn missing-images" role="status">
          导出时有 {missingImages} 张图片找不到，阅读器和 EPUB 里显示为缺图
          {!!structure.data?.missing_images.length && (
            <>
              ：
              <ul>
                {structure.data.missing_images.map((src) => (
                  <li key={src}>
                    <code>{src}</code>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
      {!active && !!t.quality_summary.total && <TermSuggestions id={id} />}
      {!active && structured && (
        <TermCorrectionPanel
          key={
            correction
              ? `${correction.term}|${correction.old}|${correction.new}`
              : "blank"
          }
          id={id}
          initial={correction}
          onApplied={refresh}
        />
      )}
      {downloadable && structured && <BookSearch taskId={id} />}
      {downloadable && structured && (
        <ReviewAgentPanel
          id={id}
          revision={chunks.data?.revision ?? null}
          onDone={refresh}
        />
      )}
      {!active && (
        <section className="section">
          <div className="section-header">
            <span className="section-title">逐块审校</span>
            <label>
              <input
                type="checkbox"
                checked={reviewOnly}
                onChange={(e) => setReviewOnly(e.target.checked)}
              />{" "}
              仅显示本页待处理块
            </label>
          </div>
          {chunks.isError ? (
            <div className="alert-banner error" role="alert">
              块结果加载失败：{errorText(chunks.error)}
              <button className="btn-outline" onClick={() => chunks.refetch()}>
                重试
              </button>
            </div>
          ) : chunks.isPending ? (
            <p>加载中…</p>
          ) : !chunks.data?.total ? (
            <p>
              暂无结构化块结果。历史任务可以下载已有文件，重新执行后支持逐块审校。
            </p>
          ) : (
            <>
              <nav className="chunk-jumps" aria-label="本页块导航">
                {chunks.data.chunks.map((c) => (
                  <a
                    key={c.chunk_index}
                    href={`#chunk-${c.chunk_index}`}
                    onClick={(e) => {
                      e.preventDefault();
                      document
                        .getElementById(`chunk-${c.chunk_index}`)
                        ?.scrollIntoView({ behavior: "smooth" });
                    }}
                  >
                    块 {c.chunk_index + 1}
                  </a>
                ))}
              </nav>
              {!visible.length && <p>本页没有待处理块。</p>}
              {visible.map((c) => (
                <ChunkEditor
                  key={`${chunks.data?.revision}-${c.chunk_index}`}
                  chunk={c}
                  id={id}
                  revision={chunks.data!.revision}
                  onSaved={refresh}
                  onTermChange={structured ? setCorrection : undefined}
                />
              ))}
              <Pagination
                current={page}
                pageSize={10}
                total={chunks.data.total}
                showSizeChanger={false}
                onChange={setPage}
              />
            </>
          )}
        </section>
      )}
    </main>
  );
}
