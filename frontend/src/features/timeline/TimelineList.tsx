import { TimelineItem } from '../../types';

type TimelineListProps = {
  items: TimelineItem[];
};

export function TimelineList({ items }: TimelineListProps) {
  const notes = items.filter((item) => item.kind === 'agent_output');
  const toolCalls = items.filter((item) => item.kind === 'tool_call');

  return (
    <section className="timeline-shell">
      <details className="run-group" open>
        <summary className="run-summary">
          <span className="run-badge">已处理 1 轮</span>
          <span className="run-duration">3m 21s</span>
        </summary>

        <div className="run-content">
          {notes.map((item) => (
            <article key={item.id} className="run-note">
              {item.summary ? <p>{item.summary}</p> : null}
              {item.detail ? <div className="run-note-meta">{item.detail}</div> : null}
            </article>
          ))}

          <details className="command-group">
            <summary className="command-group-summary">Ran {toolCalls.length} commands</summary>
            <div className="command-group-body">
              {toolCalls.map((item) => (
                <details
                  key={item.id}
                  className={`command-item ${item.status ?? 'plain'}`}
                  open={item.status === 'running'}
                >
                  <summary className="command-item-summary">
                    <span className={`status-dot ${item.status ?? 'plain'}`} />
                    <span>已运行 {item.tool ?? item.title}</span>
                  </summary>
                  <div className="command-output-shell">
                    <div className="command-output-header">
                      <span>{item.tool ?? item.title}</span>
                      <span>{item.status === 'done' ? '成功' : item.status === 'failed' ? '失败' : '进行中'}</span>
                    </div>
                    <pre className="command-output-body">
                      {item.summary ? `${item.summary}\n\n` : ''}
                      {item.detail ?? '无详细结果'}
                    </pre>
                  </div>
                </details>
              ))}
            </div>
          </details>
        </div>
      </details>
    </section>
  );
}
