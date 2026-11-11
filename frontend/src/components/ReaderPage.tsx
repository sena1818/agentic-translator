import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getTaskStatus, readerUrl, errorText } from "../services/api";

/**
 * 应用内阅读页：整页显示结构化导出的对照阅读器。
 * 地址形如 #/tasks/<id>/read?seg=41，可收藏、刷新后仍停在该页并定位到该段。
 */
export default function ReaderPage() {
  const { taskId: id = "" } = useParams();
  const [params] = useSearchParams();
  const seg = params.get("seg");
  const task = useQuery({
    queryKey: ["task", id],
    queryFn: () => getTaskStatus(id),
  });
  const structured = !!task.data?.quality_summary?.structured;
  const src = `${readerUrl(id)}${seg && /^\d+$/.test(seg) ? `#seg-${seg}` : ""}`;
  return (
    <main className="reader-page">
      <header className="reader-page-bar">
        <Link className="back-link" to={`/tasks/${id}`}>
          ← 任务详情
        </Link>
        <h1>{task.data?.filename || "对照阅读"}</h1>
        <a
          className="btn-outline"
          href={src}
          target="_blank"
          rel="noopener noreferrer"
        >
          新窗口打开
        </a>
      </header>
      {task.isError ? (
        <div className="alert-banner error" role="alert">
          无法读取任务：{errorText(task.error)}
        </div>
      ) : !task.data ? (
        <p role="status">正在读取任务…</p>
      ) : !structured ? (
        <div className="alert-banner warn" role="status">
          这个任务没有对照阅读器。历史任务可以在任务详情里下载已有文件，重新执行后支持阅读页。
        </div>
      ) : (
        <iframe
          key={src}
          className="reader-frame"
          title="对照阅读器"
          src={src}
          sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
        />
      )}
    </main>
  );
}
