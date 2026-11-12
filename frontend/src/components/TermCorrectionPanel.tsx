import { useState } from "react";
import { App } from "antd";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  applyTermCorrection,
  errorText,
  getTermSuggestions,
  previewTermCorrection,
  type PropagationPreview,
  type TermCorrection,
} from "../services/api";

const skipReasons: Record<string, string> = {
  already_new: "已是新译名",
  other_rendering: "用了别的译法",
  not_aligned: "该段没有段级译文，需手动处理",
  protected_only: "旧译名只出现在更长的译名里",
};

/** 书内纠正传播：改一个译名 → 预览全书其他出现处 → 勾选后批量应用。
 *  initial 变化时由调用方换 key 重新挂载，表单随之重置。 */
export function TermCorrectionPanel({
  id,
  initial,
  onApplied,
}: {
  id: string;
  initial?: TermCorrection | null;
  onApplied: () => void;
}) {
  const { message } = App.useApp();
  const [form, setForm] = useState<TermCorrection>(
    initial ?? { term: "", old: "", new: "" },
  );
  const [preview, setPreview] = useState<PropagationPreview | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const load = useMutation({
    mutationFn: () => previewTermCorrection(id, form),
    onSuccess: (data) => {
      setPreview(data);
      setSelected(new Set(data.items.map((item) => item.position)));
    },
    onError: (e) => message.error(errorText(e)),
  });
  const apply = useMutation({
    mutationFn: () =>
      applyTermCorrection(id, form, [...selected], preview?.revision ?? null),
    onSuccess: (data) => {
      message.success(
        `已在全书替换 ${data.replacements} 处，改动的块已重新质检`,
      );
      setPreview(null);
      onApplied();
    },
    onError: (e) => message.error(errorText(e)),
  });
  const ready = form.term.trim() && form.old.trim() && form.new.trim();
  const toggle = (position: number) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(position)) next.delete(position);
      else next.add(position);
      return next;
    });
  return (
    <section className="section term-correction" aria-label="全书改译名">
      <div className="section-header">
        <span className="section-title">全书改译名</span>
      </div>
      <div className="term-correction-form">
        {(
          [
            ["term", "术语原文"],
            ["old", "旧译名"],
            ["new", "新译名"],
          ] as const
        ).map(([key, label]) => (
          <label key={key}>
            {label}
            <input
              aria-label={label}
              value={form[key]}
              onChange={(e) => setForm({ ...form, [key]: e.target.value })}
            />
          </label>
        ))}
        <button
          className="btn-outline"
          disabled={!ready || load.isPending}
          onClick={() => load.mutate()}
        >
          预览出现处
        </button>
      </div>
      {preview && (
        <div className="term-correction-preview">
          <p>
            原文含「{preview.term}」、译文仍用「{preview.old}」的段：
            {preview.total_segments} 段，共 {preview.total_replacements} 处
            {!!preview.skipped.length &&
              `；另有 ${preview.skipped.length} 段不改`}
          </p>
          {!preview.items.length ? (
            <p>没有需要替换的段。</p>
          ) : (
            <ul>
              {preview.items.map((item) => (
                <li key={item.position}>
                  <label>
                    <input
                      type="checkbox"
                      aria-label={`应用到段 ${item.position + 1}`}
                      checked={selected.has(item.position)}
                      onChange={() => toggle(item.position)}
                    />{" "}
                    <span className="term-correction-source">
                      {item.source}
                    </span>
                    <br />
                    <del>{item.translation}</del> → <ins>{item.preview}</ins>
                  </label>
                </li>
              ))}
            </ul>
          )}
          {!!preview.skipped.length && (
            <details>
              <summary>不改的段</summary>
              <ul>
                {preview.skipped.map((item) => (
                  <li key={item.position}>
                    段 {item.position + 1}：
                    {skipReasons[item.reason] ?? item.reason}
                  </li>
                ))}
              </ul>
            </details>
          )}
          <button
            className="btn-accent"
            disabled={!selected.size || apply.isPending}
            onClick={() => apply.mutate()}
          >
            应用到选中的 {selected.size} 段
          </button>
        </div>
      )}
    </section>
  );
}

/** 同作者、同领域其他书的术语建议（只作参考，不参与术语校验）。 */
export function TermSuggestions({ id }: { id: string }) {
  const suggestions = useQuery({
    queryKey: ["term-suggestions", id],
    queryFn: () => getTermSuggestions(id),
  });
  const items = suggestions.data?.items ?? [];
  if (!items.length) return null;
  return (
    <details className="term-suggestions">
      <summary>来自其他书的术语建议 {items.length} 条（仅作参考）</summary>
      <ul>
        {items.map((item) => (
          <li key={item.term}>
            {item.term} → {item.rendering}
            <span className="term-suggestion-source">（{item.label}）</span>
          </li>
        ))}
      </ul>
    </details>
  );
}
