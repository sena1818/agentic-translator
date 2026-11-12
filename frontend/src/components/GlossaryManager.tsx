import React, { useState } from "react";
import { Modal, Form, Input, Upload, App } from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getGlossaryList,
  getGlossary,
  createGlossary,
  modifyGlossaryTerms,
  deleteGlossary,
  exportGlossary,
  importGlossary,
  type Glossary,
  type GlossaryCandidate,
  suggestGlossary,
  errorText,
} from "../services/api";
import TermReviewList from "./TermReviewList";

const GlossaryManager: React.FC = () => {
  const { message, modal } = App.useApp();
  const queryClient = useQueryClient();

  const [search, setSearch] = useState("");
  const [draftOpen, setDraftOpen] = useState(false);
  const [draftFile, setDraftFile] = useState<File | null>(null);
  const [candidates, setCandidates] = useState<GlossaryCandidate[]>([]);
  const [draftName, setDraftName] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [isImportOpen, setIsImportOpen] = useState(false);
  const [sourceInput, setSourceInput] = useState("");
  const [targetInput, setTargetInput] = useState("");
  const [createForm] = Form.useForm();
  const [importForm] = Form.useForm();

  // ── Queries ──
  const {
    data: glossaries = [],
    isLoading,
    isError: listError,
    refetch: reloadList,
  } = useQuery<Glossary[]>({
    queryKey: ["glossaries"],
    queryFn: getGlossaryList,
  });

  const {
    data: selectedData,
    isError: detailError,
    refetch: reloadDetail,
  } = useQuery<Glossary>({
    queryKey: ["glossary", selectedId],
    queryFn: () => getGlossary(selectedId!),
    enabled: !!selectedId,
  });

  // ── Mutations ──
  const createMutation = useMutation({
    mutationFn: ({
      name,
      terms,
    }: {
      name: string;
      terms: Record<string, string>;
    }) => createGlossary(name, terms),
    onSuccess: () => {
      message.success("术语表创建成功");
      queryClient.invalidateQueries({ queryKey: ["glossaries"] });
      setIsCreateOpen(false);
      createForm.resetFields();
    },
    onError: (err: Error) => message.error(`创建失败: ${errorText(err)}`),
  });

  const modifyMutation = useMutation({
    mutationFn: ({
      glossaryId,
      add,
      remove,
    }: {
      glossaryId: string;
      add?: Record<string, string>;
      remove?: string[];
    }) => modifyGlossaryTerms(glossaryId, add, remove),
    onSuccess: () => {
      message.success("术语已更新");
      queryClient.invalidateQueries({ queryKey: ["glossary", selectedId] });
      queryClient.invalidateQueries({ queryKey: ["glossaries"] });
    },
    onError: (err: Error) => message.error(`更新失败: ${errorText(err)}`),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteGlossary,
    onSuccess: () => {
      message.success("术语表已删除");
      queryClient.invalidateQueries({ queryKey: ["glossaries"] });
      setSelectedId(null);
    },
    onError: (err: Error) => message.error(`删除失败: ${errorText(err)}`),
  });

  const importMutation = useMutation({
    mutationFn: ({ file, name }: { file: File; name: string }) =>
      importGlossary(file, name),
    onSuccess: () => {
      message.success("术语表导入成功");
      queryClient.invalidateQueries({ queryKey: ["glossaries"] });
      setIsImportOpen(false);
      importForm.resetFields();
    },
    onError: (err: Error) => message.error(`导入失败: ${errorText(err)}`),
  });

  const draftMutation = useMutation({
    mutationFn: () => suggestGlossary(draftFile!),
    onSuccess: setCandidates,
    onError: (err) => message.error(errorText(err)),
  });
  const saveDraft = useMutation({
    mutationFn: () =>
      createGlossary(
        draftName.trim(),
        Object.fromEntries(
          candidates.map((c) => [c.term.trim(), c.translation.trim()]),
        ),
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["glossaries"] });
      setDraftOpen(false);
      setCandidates([]);
      message.success("术语草稿已保存");
    },
    onError: (err) => message.error(errorText(err)),
  });

  // ── Handlers ──
  const handleCreate = () => {
    createForm.validateFields().then((values) => {
      try {
        const raw = (values.terms || "").trim();
        let terms: Record<string, string> = {};
        if (raw.startsWith("{")) terms = JSON.parse(raw);
        else if (raw) {
          terms = Object.fromEntries(
            raw
              .split("\n")
              .filter(Boolean)
              .map((line: string) => {
                const pair = line.split("\t");
                if (pair.length !== 2 || !pair[0].trim() || !pair[1].trim())
                  throw new Error("每行需要原文和译文两列");
                return pair.map((s) => s.trim());
              }),
          );
        }
        createMutation.mutate({ name: values.name, terms });
      } catch {
        message.error(
          "格式无效：请粘贴两列术语（制表符分隔），或使用 JSON 对象",
        );
      }
    });
  };

  const handleAddTerm = () => {
    if (!selectedId || !sourceInput.trim() || !targetInput.trim()) {
      message.warning("请填写原文和译文");
      return;
    }
    modifyMutation.mutate(
      {
        glossaryId: selectedId,
        add: { [sourceInput.trim()]: targetInput.trim() },
      },
      {
        onSuccess: () => {
          setSourceInput("");
          setTargetInput("");
        },
      },
    );
  };

  const handleRemoveTerm = (source: string) => {
    if (!selectedId) return;
    modal.confirm({
      title: "删除术语",
      content: `确定删除术语 "${source}"？`,
      okText: "确定",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () =>
        modifyMutation.mutate({ glossaryId: selectedId, remove: [source] }),
    });
  };

  const handleDeleteGlossary = (id: string, name: string) => {
    modal.confirm({
      title: "删除术语表",
      content: `确定删除术语表 "${name}"？此操作不可恢复。`,
      okText: "确定删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => deleteMutation.mutate(id),
    });
  };

  const handleImport = () => {
    importForm.validateFields().then((values) => {
      if (!values.file?.length) {
        message.error("请选择文件");
        return;
      }
      const file = values.file[0].originFileObj as File;
      importMutation.mutate({ file, name: values.name });
    });
  };

  // ── Terms data ──
  const termEntries = selectedData?.terms
    ? Object.entries(selectedData.terms).filter(([source, target]) =>
        `${source} ${target}`.toLowerCase().includes(search.toLowerCase()),
      )
    : [];

  const formatDate = (str?: string) =>
    str ? new Date(str).toLocaleDateString("zh-CN") : "—";

  return (
    <div className="page">
      {/* ── Hero ── */}
      <div className="hero" style={{ marginBottom: 40 }}>
        <div className="hero-text">
          <h1>
            术语表<em>管理</em>
          </h1>
          <p>统一管理翻译术语，确保专业词汇在全文中保持一致的译名。</p>
        </div>
        <div style={{ display: "flex", gap: 10, alignSelf: "flex-end" }}>
          <button
            className="btn-outline"
            onClick={() => {
              setDraftOpen(true);
              setCandidates([]);
            }}
          >
            从文档生成草稿
          </button>
          <button className="btn-outline" onClick={() => setIsImportOpen(true)}>
            导入 JSON
          </button>
          <button className="btn-accent" onClick={() => setIsCreateOpen(true)}>
            + 新建术语表
          </button>
        </div>
      </div>

      <div className="rule" />

      <TermReviewList />

      {/* ── Main Layout ── */}
      <div className="glossary-layout">
        {/* Sidebar */}
        <div className="glossary-sidebar">
          <div className="glossary-sidebar-header">
            <span className="glossary-sidebar-title">术语表</span>
            <span
              style={{
                fontFamily: "DM Mono, monospace",
                fontSize: 11,
                color: "var(--text-3)",
                fontWeight: 300,
              }}
            >
              共 {glossaries.length} 个
            </span>
          </div>

          {listError ? (
            <div className="glossary-empty" role="alert">
              术语表加载失败。
              <button className="btn-outline" onClick={() => reloadList()}>
                重试
              </button>
            </div>
          ) : isLoading ? (
            <div className="glossary-empty">加载中…</div>
          ) : glossaries.length === 0 ? (
            <div className="glossary-empty">暂无术语表</div>
          ) : (
            glossaries.map((g) => (
              <button
                type="button"
                key={g.id}
                className={`glossary-item ${selectedId === g.id ? "active" : ""}`}
                onClick={() => setSelectedId(g.id)}
              >
                <div className="glossary-item-name">{g.name}</div>
                <div className="glossary-item-meta">
                  {g.term_count ?? 0} 个术语 · {formatDate(g.updated_at)}
                </div>
              </button>
            ))
          )}
        </div>

        {/* Detail Panel */}
        <div className="glossary-detail">
          {!selectedId ? (
            <div className="glossary-empty">← 从左侧选择一个术语表查看详情</div>
          ) : detailError ? (
            <div className="glossary-empty" role="alert">
              读取术语表失败。
              <button className="btn-outline" onClick={() => reloadDetail()}>
                重试
              </button>
            </div>
          ) : !selectedData ? (
            <div className="glossary-empty">加载中…</div>
          ) : (
            <>
              <div className="glossary-detail-header">
                <span className="glossary-detail-title">
                  {selectedData.name}
                </span>
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    className="btn-outline"
                    onClick={() =>
                      window.open(exportGlossary(selectedId), "_blank")
                    }
                  >
                    导出 JSON
                  </button>
                  <button
                    className="btn-outline"
                    style={{
                      color: "var(--red)",
                      borderColor: "rgba(139,32,32,0.3)",
                    }}
                    onClick={() =>
                      handleDeleteGlossary(selectedId, selectedData.name)
                    }
                  >
                    删除
                  </button>
                </div>
              </div>

              <div className="glossary-search">
                <input
                  className="term-input"
                  aria-label="搜索术语"
                  placeholder="搜索原文或译文"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
              {/* Add Term Row */}
              <div className="add-term-form">
                <input
                  className="term-input"
                  placeholder="原文 (English)"
                  value={sourceInput}
                  onChange={(e) => setSourceInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleAddTerm()}
                />
                <input
                  className="term-input"
                  placeholder="译文 (中文)"
                  value={targetInput}
                  onChange={(e) => setTargetInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleAddTerm()}
                />
                <button
                  className="btn-accent"
                  onClick={handleAddTerm}
                  disabled={modifyMutation.isPending}
                >
                  {selectedData.terms?.[sourceInput.trim()]
                    ? "更新译名"
                    : "添加"}
                </button>
              </div>

              {/* Terms Table */}
              {termEntries.length === 0 ? (
                <div className="glossary-empty">暂无术语，从上方添加</div>
              ) : (
                <table className="terms-table">
                  <thead>
                    <tr>
                      <th>原文 (English)</th>
                      <th>译文 (中文)</th>
                      <th style={{ width: 60 }}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {termEntries.map(([source, target]) => (
                      <tr key={source}>
                        <td className="term-en">{source}</td>
                        <td className="term-zh">{target}</td>
                        <td>
                          <button
                            className="btn-del"
                            onClick={() => handleRemoveTerm(source)}
                            style={{ fontSize: 12, padding: "4px 8px" }}
                          >
                            删除
                          </button>
                          <button
                            className="btn-outline"
                            onClick={() => {
                              setSourceInput(source);
                              setTargetInput(target);
                            }}
                          >
                            编辑
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </div>
      </div>

      <Modal
        title="生成术语草稿"
        open={draftOpen}
        onCancel={() => setDraftOpen(false)}
        footer={null}
        width={800}
      >
        <p>选择文档生成候选术语，审阅译名后保存为新的术语表。</p>
        <input
          type="file"
          aria-label="术语草稿来源文档"
          accept=".md,.pdf,.epub"
          onChange={(e) => setDraftFile(e.target.files?.[0] || null)}
        />
        <button
          className="btn-accent"
          disabled={!draftFile || draftMutation.isPending}
          onClick={() => draftMutation.mutate()}
        >
          {draftMutation.isPending ? "正在提取…" : "提取候选术语"}
        </button>
        {!!candidates.length && (
          <>
            <div className="draft-candidates">
              {candidates.map((c, i) => (
                <div className="draft-candidate" key={i}>
                  <input
                    aria-label={`候选 ${i + 1} 原文`}
                    value={c.term}
                    onChange={(e) =>
                      setCandidates((old) =>
                        old.map((item, n) =>
                          n === i ? { ...item, term: e.target.value } : item,
                        ),
                      )
                    }
                  />
                  <input
                    aria-label={`候选 ${i + 1} 译文`}
                    value={c.translation}
                    onChange={(e) =>
                      setCandidates((old) =>
                        old.map((item, n) =>
                          n === i
                            ? { ...item, translation: e.target.value }
                            : item,
                        ),
                      )
                    }
                  />
                  <button
                    className="btn-del"
                    onClick={() =>
                      setCandidates((old) => old.filter((_, n) => n !== i))
                    }
                  >
                    移除
                  </button>
                  <small>{c.note}</small>
                </div>
              ))}
            </div>
            <Input
              aria-label="草稿术语表名称"
              placeholder="新术语表名称"
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
            />
            <button
              className="btn-accent"
              disabled={
                !draftName.trim() ||
                saveDraft.isPending ||
                candidates.some((c) => !c.term.trim() || !c.translation.trim())
              }
              onClick={() => saveDraft.mutate()}
            >
              保存审阅后的草稿
            </button>
          </>
        )}
      </Modal>

      {/* ── Create Modal ── */}
      <Modal
        title="新建术语表"
        open={isCreateOpen}
        onOk={handleCreate}
        onCancel={() => {
          setIsCreateOpen(false);
          createForm.resetFields();
        }}
        confirmLoading={createMutation.isPending}
        okText="创建"
        cancelText="取消"
      >
        <Form form={createForm} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item
            name="name"
            label="术语表名称"
            rules={[{ required: true, message: "请输入名称" }]}
          >
            <Input placeholder="例如：Cyclonopedia 术语表" />
          </Form.Item>
          <Form.Item
            name="terms"
            label="术语（可粘贴 Excel 两列，也支持 JSON；可留空）"
          >
            <Input.TextArea
              rows={7}
              placeholder={
                '{\n  "Hyperstition": "超虚构 (Hyperstition)",\n  "War Machine": "战争机器 (War Machine)"\n}'
              }
              style={{ fontFamily: "DM Mono, monospace", fontSize: 13 }}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* ── Import Modal ── */}
      <Modal
        title="导入术语表"
        open={isImportOpen}
        onOk={handleImport}
        onCancel={() => {
          setIsImportOpen(false);
          importForm.resetFields();
        }}
        confirmLoading={importMutation.isPending}
        okText="导入"
        cancelText="取消"
      >
        <Form form={importForm} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item
            name="name"
            label="术语表名称"
            rules={[{ required: true, message: "请输入名称" }]}
          >
            <Input placeholder="例如：导入的术语表" />
          </Form.Item>
          <Form.Item
            name="file"
            label="选择 JSON 文件"
            valuePropName="fileList"
            getValueFromEvent={(e) => (Array.isArray(e) ? e : e?.fileList)}
            rules={[{ required: true, message: "请选择文件" }]}
          >
            <Upload beforeUpload={() => false} maxCount={1} accept=".json">
              <button className="btn-outline" type="button">
                选择文件
              </button>
            </Upload>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default GlossaryManager;
