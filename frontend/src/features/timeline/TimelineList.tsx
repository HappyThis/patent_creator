import { ProcessEvent } from '../../types';

type TimelineListProps = {
  items: ProcessEvent[];
};

export function TimelineList({ items }: TimelineListProps) {
  const notes = items.filter((item) => item.kind === 'agent_output');
  const toolCalls = items.filter((item) => item.kind === 'tool_call');

  return (
    <details className="process-group">
      <summary className="process-summary">
        <span>执行过程</span>
        <span className="process-summary-meta">{toolCalls.length} 个工具调用</span>
      </summary>

      <div className="process-body">
        {notes.map((item) => (
          <article key={item.id} className="run-note">
            {item.summary ? <p>{item.summary}</p> : null}
            {item.detail ? <div className="run-note-meta">{item.detail}</div> : null}
          </article>
        ))}

        <div className="command-group-body">
          {toolCalls.map((item) => (
            <details
              key={item.id}
              className={`command-item ${item.status ?? 'plain'}`}
              open={item.status === 'running'}
            >
              <summary className="command-item-summary">
                <span className={`status-dot ${item.status ?? 'plain'}`} />
                <span>{item.tool ?? item.title}</span>
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
      </div>
    </details>
  );
}
