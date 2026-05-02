import type { ContextUsageSummary } from '../../types';

type ContextUsageBadgeProps = {
  contextUsage?: ContextUsageSummary | null;
};

export function ContextUsageBadge({ contextUsage }: ContextUsageBadgeProps) {
  if (!contextUsage) {
    return null;
  }

  const contextPercent = Math.round(contextUsage.used_ratio * 100);
  const contextBarWidth = Math.min(100, Math.max(0, contextPercent));

  return (
    <div
      className={`composer-context ${contextUsage.status}`}
      tabIndex={0}
      aria-label={`上下文已用 ${contextPercent}%`}
    >
      <span className="context-ring" aria-hidden="true" />
      <span className="context-pill-value">{contextPercent}%</span>
      <div className="context-popover" role="tooltip">
        <div className="context-popover-header">
          <span>
            <span className="context-popover-label">上下文</span>
            <strong>用量详情</strong>
          </span>
          <b>{contextPercent}%</b>
        </div>
        <div className="context-popover-bar" aria-hidden="true">
          <span style={{ width: `${contextBarWidth}%` }} />
        </div>
        <dl className="context-popover-stats">
          <div>
            <dt>已用</dt>
            <dd>{formatCompactTokens(contextUsage.used_tokens)} 标记</dd>
          </div>
          <div>
            <dt>上限</dt>
            <dd>{formatCompactTokens(contextUsage.max_tokens)} 标记</dd>
          </div>
        </dl>
        <p>{contextUsage.status === 'over_limit' ? '接近上限，系统将压缩早期上下文' : '接近上限时会自动压缩'}</p>
      </div>
    </div>
  );
}

function formatCompactTokens(value: number): string {
  if (value >= 1000) {
    return `${Math.round(value / 1000)}k`;
  }
  return String(value);
}
