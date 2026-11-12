import React from "react";
import { useQuery } from "@tanstack/react-query";
import { getTermReviews, type TermReview } from "../services/api";

/** 术语 agent 拿不准、转交人工的条目。审阅后在对应书的"术语账本"里补上译名，下次运行生效。 */
const TermReviewList: React.FC = () => {
  const { data: items = [] } = useQuery<TermReview[]>({
    queryKey: ["term-reviews"],
    queryFn: getTermReviews,
  });
  if (!items.length) return null;
  return (
    <details className="term-reviews">
      <summary>术语 agent 转交待审（{items.length}）</summary>
      <p className="term-review-hint">
        这些词查证后仍拿不准，没有并入术语表。确定译名后，在对应书的「术语账本」里补上，下次运行生效。
      </p>
      <ul>
        {items.map((item) => (
          <li key={`${item.task_id}-${item.term}`}>
            <strong>{item.term}</strong>
            <span className="term-review-source">
              《{item.book}》· {item.reason || "未说明原因"}
            </span>
            {item.contexts.length > 0 && (
              <ol>
                {item.contexts.map((context, index) => (
                  <li key={index}>{context}</li>
                ))}
              </ol>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
};

export default TermReviewList;
