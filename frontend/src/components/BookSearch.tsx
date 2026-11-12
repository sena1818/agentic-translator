import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import {
  errorText,
  readerPagePath,
  searchBookByMeaning,
  type MeaningSearchResult,
} from "../services/api";

/** 审校页的"按意思找原话"：一句大概的话（中文或英文）→ 意思相近的段，点击跳到对照阅读位置。 */
export default function BookSearch({ taskId }: { taskId: string }) {
  const [query, setQuery] = useState("");
  const search = useMutation<MeaningSearchResult, unknown, string>({
    mutationFn: (q) => searchBookByMeaning(taskId, q),
  });
  const result = search.data;

  return (
    <section className="section book-search" aria-label="按意思检索">
      <div className="section-header">
        <span className="section-title">按意思找原话</span>
      </div>
      <form
        className="book-search-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (query.trim()) search.mutate(query.trim());
        }}
      >
        <input
          type="search"
          value={query}
          placeholder="用一句大概的话描述，中文或英文都可以"
          aria-label="检索内容"
          onChange={(e) => setQuery(e.target.value)}
        />
        <button
          type="submit"
          className="btn-outline"
          disabled={!query.trim() || search.isPending}
        >
          {search.isPending ? "检索中…" : "检索"}
        </button>
      </form>
      {search.isError && (
        <div className="alert-banner warn" role="alert">
          {errorText(search.error)}
        </div>
      )}
      {result && (
        <>
          {result.mode === "lexical" && result.hint && (
            <p className="book-search-hint" role="status">
              {result.hint}
            </p>
          )}
          {result.items.length === 0 ? (
            <p className="book-search-empty">没有找到相近的段落。</p>
          ) : (
            <ol className="book-search-results">
              {result.items.map((hit) => (
                <li key={hit.position}>
                  {hit.section_path.length > 0 && (
                    <div className="book-search-section">
                      {hit.section_path.join(" › ")}
                    </div>
                  )}
                  <p className="book-search-source" lang="en">
                    {hit.source}
                  </p>
                  {hit.translation && (
                    <p className="book-search-translation" lang="zh">
                      {hit.translation}
                    </p>
                  )}
                  {hit.seg_id !== null ? (
                    <Link to={readerPagePath(taskId, hit.seg_id)}>
                      在对照阅读中定位
                    </Link>
                  ) : (
                    <span className="book-search-note">
                      保留原文的段，阅读器中无对照位置
                    </span>
                  )}
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </section>
  );
}
