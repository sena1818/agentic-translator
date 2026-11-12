import { useState } from "react";
import { App, Pagination } from "antd";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createTranslationTask,
  getTaskList,
  deleteTask,
  getGlossaryList,
  getSystemState,
  errorText,
} from "../services/api";

import {
  statusLabels,
  chipClass,
  stageLabels,
} from "../services/taskPresentation";

export default function TranslationPage() {
  const { message, modal } = App.useApp();
  const client = useQueryClient();
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [glossary, setGlossary] = useState("");
  const [bilingual, setBilingual] = useState(true);
  const [author, setAuthor] = useState("");
  const [domain, setDomain] = useState("");
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const systems = useQuery({
    queryKey: ["system"],
    queryFn: getSystemState,
    refetchInterval: 10000,
  });
  const glossaries = useQuery({
    queryKey: ["glossaries"],
    queryFn: getGlossaryList,
  });
  const tasks = useQuery({
    queryKey: ["tasks", page, search, status],
    queryFn: () => getTaskList((page - 1) * 20, 20, search, status),
    refetchInterval: (query) =>
      query.state.data?.tasks.some((t) =>
        ["pending", "processing"].includes(t.status),
      )
        ? 3000
        : 10000,
  });
  const create = useMutation({
    mutationFn: () =>
      createTranslationTask(file!, glossary || undefined, bilingual, {
        author,
        domain,
      }),
    onSuccess: (task) => {
      setFile(null);
      client.invalidateQueries({ queryKey: ["tasks"] });
      navigate(`/tasks/${task.task_id}`);
    },
    onError: (error) => message.error(errorText(error)),
  });
  const remove = useMutation({
    mutationFn: deleteTask,
    onSuccess: () => client.invalidateQueries({ queryKey: ["tasks"] }),
    onError: (error) => message.error(errorText(error)),
  });
  function choose(f?: File) {
    if (!f) return;
    const formats = systems.data?.formats || ["md"];
    const ext = f.name.split(".").pop()?.toLowerCase();
    if (!ext || !formats.includes(ext)) {
      message.error(`当前服务支持 ${formats.join("、")} 格式`);
      return;
    }
    if (
      !f.size ||
      f.size > (systems.data?.max_upload_bytes || 50 * 1024 * 1024)
    ) {
      message.error("文件不能为空，且不得超过 50 MB");
      return;
    }
    setFile(f);
  }
  const stats = tasks.data?.stats || {};
  return (
    <main className="page">
      <div className="workspace-heading">
        <div>
          <h1>翻译任务</h1>
          <p>上传文档，跟踪执行进度，并按块审校译文。</p>
        </div>
        <div className="workspace-summary">
          <span>
            <strong>{stats.completed || 0}</strong> 已完成
          </span>
          <span>
            <strong>{stats.processing || 0}</strong> 执行中
          </span>
          <span>
            <strong>{stats.partial_success || 0}</strong> 需处理
          </span>
        </div>
      </div>
      {systems.isError && (
        <div className="alert-banner error" role="alert">
          无法连接服务，请确认 API 已启动。
          <button className="btn-outline" onClick={() => systems.refetch()}>
            重新连接
          </button>
        </div>
      )}
      {systems.data?.workers_online === 0 && (
        <div className="alert-banner warn" role="status">
          Worker 离线；新任务会进入队列，启动 worker 后继续执行。
        </div>
      )}
      <section className="section" aria-label="新建翻译任务">
        <div className="section-header">
          <span className="section-title">新建任务</span>
        </div>
        <div className="upload-grid">
          <div
            className={`dropzone ${dragging ? "dragging" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              choose(e.dataTransfer.files[0]);
            }}
          >
            <label htmlFor="translation-file" className="drop-title">
              {file ? file.name : "拖放文档，或选择文件"}
            </label>
            <input
              id="translation-file"
              type="file"
              accept={(systems.data?.formats || ["md"])
                .map((f) => `.${f}`)
                .join(",")}
              onChange={(e) => {
                choose(e.target.files?.[0]);
                e.target.value = "";
              }}
            />
            <div className="drop-hint">
              {file
                ? `${(file.size / 1024).toFixed(1)} KB`
                : `支持 ${(systems.data?.formats || ["md"]).join(" / ").toUpperCase()} · 最大 50 MB`}
            </div>
            {file && (
              <button className="btn-outline" onClick={() => setFile(null)}>
                移除文件
              </button>
            )}
          </div>
          <div className="config-panel">
            <label className="field-label" htmlFor="glossary-select">
              术语表
            </label>
            <div className="select-wrap">
              <select
                id="glossary-select"
                value={glossary}
                onChange={(e) => setGlossary(e.target.value)}
              >
                <option value="">不使用术语表</option>
                {glossaries.data?.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}（{g.term_count} 个术语）
                  </option>
                ))}
              </select>
            </div>
            {glossaries.isError && (
              <p role="alert">
                术语表加载失败。
                <button
                  className="btn-outline"
                  onClick={() => glossaries.refetch()}
                >
                  重试
                </button>
              </p>
            )}
            <label className="field-label" htmlFor="book-author">
              作者（可选）
            </label>
            <input
              id="book-author"
              className="text-input"
              placeholder="同作者的书会互相给出术语建议"
              value={author}
              onChange={(e) => setAuthor(e.target.value)}
            />
            <label className="field-label" htmlFor="book-domain">
              领域（可选）
            </label>
            <input
              id="book-domain"
              className="text-input"
              placeholder="如 哲学、机器学习"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
            />
            <label className="toggle-row">
              <span className="toggle-info">
                <span className="toggle-name">双语对照</span>
                <span className="toggle-desc">同时保留原文与译文</span>
              </span>
              <input
                type="checkbox"
                checked={bilingual}
                onChange={(e) => setBilingual(e.target.checked)}
              />
            </label>
            <button
              className="btn-submit"
              disabled={!file || create.isPending || systems.isError}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "正在上传…" : "创建翻译任务"}
            </button>
          </div>
        </div>
      </section>
      <section className="section" aria-label="历史任务">
        <div className="section-header">
          <span className="section-title">任务记录</span>
          <span>{tasks.data?.total ?? 0} 个匹配任务</span>
        </div>
        <div className="list-filters">
          <input
            aria-label="搜索文件名"
            placeholder="搜索文件名"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
          />
          <select
            aria-label="筛选任务状态"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setPage(1);
            }}
          >
            <option value="">全部状态</option>
            {Object.entries(statusLabels).map(([key, label]) => (
              <option value={key} key={key}>
                {label}
              </option>
            ))}
          </select>
          <button
            className="btn-outline"
            onClick={() => tasks.refetch()}
            disabled={tasks.isFetching}
          >
            刷新
          </button>
        </div>
        {tasks.isError ? (
          <div className="alert-banner error" role="alert">
            任务列表加载失败：{errorText(tasks.error)}
            <button className="btn-outline" onClick={() => tasks.refetch()}>
              重试
            </button>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="task-table">
              <thead>
                <tr>
                  <th>文件名</th>
                  <th>状态 / 阶段</th>
                  <th>进度</th>
                  <th>创建时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {tasks.isPending ? (
                  <tr>
                    <td colSpan={5}>加载中…</td>
                  </tr>
                ) : !tasks.data?.tasks.length ? (
                  <tr>
                    <td colSpan={5}>没有匹配的任务</td>
                  </tr>
                ) : (
                  tasks.data.tasks.map((task) => (
                    <tr key={task.task_id}>
                      <td>
                        <Link
                          className="td-filename"
                          to={`/tasks/${task.task_id}`}
                        >
                          {task.filename}
                        </Link>
                        <div className="td-meta">
                          {task.bilingual ? "双语对照" : "中文译文"}
                        </div>
                      </td>
                      <td>
                        <span className={chipClass(task.status)}>
                          {statusLabels[task.status]}
                        </span>
                        <div className="td-meta">
                          {stageLabels[task.stage] || task.stage}
                        </div>
                      </td>
                      <td>
                        <progress max={100} value={task.progress.percentage} />
                        <div className="td-meta">
                          {task.progress.current} / {task.progress.total} 块
                        </div>
                      </td>
                      <td className="td-time">
                        {new Date(task.created_at).toLocaleString("zh-CN")}
                      </td>
                      <td>
                        <div className="td-actions">
                          <Link
                            className="btn-dl-md"
                            to={`/tasks/${task.task_id}`}
                          >
                            查看
                          </Link>
                          {!["pending", "processing"].includes(task.status) && (
                            <button
                              className="btn-del"
                              disabled={remove.isPending}
                              onClick={() =>
                                modal.confirm({
                                  title: "删除任务及文件",
                                  content: `确定删除「${task.filename}」？删除后无法恢复。`,
                                  okText: "删除",
                                  cancelText: "保留",
                                  okButtonProps: { danger: true },
                                  onOk: () => remove.mutateAsync(task.task_id),
                                })
                              }
                            >
                              删除
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
        <Pagination
          current={page}
          pageSize={20}
          total={tasks.data?.total || 0}
          showSizeChanger={false}
          onChange={setPage}
        />
      </section>
    </main>
  );
}
