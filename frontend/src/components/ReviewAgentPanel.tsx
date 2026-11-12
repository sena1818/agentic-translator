import { App } from "antd";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  errorText,
  getReviewAgent,
  readerPagePath,
  runReviewAgent,
  type TriageItem,
} from "../services/api";

const order: TriageItem["verdict"][] = ["handoff", "fixed", "dismissed"];

/** 审校 agent 分诊：对质检后仍被标记的问题，已修 / 已驳回 / 待人工。
 *  配置里没开（review_agent.enabled）时不显示。 */
export default function ReviewAgentPanel({
  id,
  revision,
  onDone,
}: {
  id: string;
  revision: string | null;
  onDone: () => void;
}) {
  const { message } = App.useApp();
  const client = useQueryClient();
  const status = useQuery({
    queryKey: ["review-agent", id],
    queryFn: () => getReviewAgent(id),
  });
  const run = useMutation({
    mutationFn: () => runReviewAgent(id, revision),
    onSuccess: (data) => {
      const v = data.summary.by_verdict;
      message.success(
        `分诊完成：已修 ${v.fixed}，已驳回 ${v.dismissed}，待人工 ${v.handoff}`,
      );
      client.invalidateQueries({ queryKey: ["review-agent", id] });
      onDone();
    },
    onError: (e) => message.error(errorText(e)),
  });
  if (!status.data?.enabled) return null;
  const last = status.data.last_run;
  const items = last
    ? [...last.results].sort(
        (a, b) => order.indexOf(a.verdict) - order.indexOf(b.verdict),
      )
    : [];
  return (
    <section className="section review-agent" aria-label="审校 agent 分诊">
      <div className="section-header">
        <span className="section-title">审校 agent 分诊</span>
        <button
          className="btn-outline"
          disabled={run.isPending}
          onClick={() => run.mutate()}
        >
          {run.isPending ? "分诊中…" : last ? "重新分诊" : "运行分诊"}
        </button>
      </div>
      {!last ? (
        <p className="review-agent-note">
          对质检后仍被标记的问题逐条分诊：能修的修（须通过逐类型不退步检查）、误报的驳回并写明理由、拿不准的转给人工。
        </p>
      ) : (
        <>
          <p className="review-agent-note">
            {last.summary.issues} 条问题：已修 {last.summary.by_verdict.fixed}
            ，已驳回 {last.summary.by_verdict.dismissed}，待人工{" "}
            {last.summary.by_verdict.handoff}；共 {last.summary.tokens} token，
            单条最多 {last.summary.max_tokens_per_issue}。
          </p>
          <ul className="review-agent-list">
            {items.map((item) => (
              <li key={item.issue.issue_id}>
                <span className={`chip review-${item.verdict}`}>
                  {item.label}
                </span>{" "}
                <Link to={readerPagePath(id, item.issue.seg_id)}>
                  段 {item.issue.seg_id}
                </Link>{" "}
                {item.issue.message}
                {item.verdict === "fixed" ? (
                  <div className="review-agent-detail">
                    <del>{item.before}</del> → <ins>{item.after}</ins>
                  </div>
                ) : (
                  item.reason && (
                    <div className="review-agent-detail">{item.reason}</div>
                  )
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
