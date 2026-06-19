import { ToolCallEvent } from '../../types';

type TimelineListProps = {
  items: ToolCallEvent[];
  label?: string;
  defaultOpen?: boolean;
};

export function TimelineList({ items, label = '执行过程', defaultOpen = false }: TimelineListProps) {
  return (
    <details className="process-group" open={defaultOpen}>
      <summary className="process-summary">
        <span>{label}</span>
        <span className="process-summary-meta">{items.length} 个工具调用</span>
      </summary>

      <div className="process-body">
        <div className="command-group-body">
          {items.map((item) => renderToolCall(item))}
        </div>
      </div>
    </details>
  );
}

function renderToolCall(item: ToolCallEvent) {
  return (
    <details
      key={item.id}
      className={`command-item ${item.status ?? 'plain'}`}
      open={item.status === 'running'}
    >
      <summary className="command-item-summary">
        <span className={`status-dot ${item.status ?? 'plain'}`} />
        <span className="command-label">
          <span className="command-title">{item.tool ?? item.title}</span>
          <span className="command-description">{describeTool(item.tool ?? item.title)}</span>
        </span>
      </summary>
      <div className="command-output-shell">
        <div className="command-output-header">
          <span>{formatToolScope(item)}</span>
          <span>{item.status === 'done' ? '成功' : item.status === 'failed' ? '失败' : '进行中'}</span>
        </div>
        <pre className="command-output-body">
          {item.summary ? `${item.summary}\n\n` : ''}
          {item.detail ?? '无详细结果'}
        </pre>
      </div>
    </details>
  );
}

function describeTool(toolName: string): string {
  const descriptions: Record<string, string> = {
    disclosure_outline: '读取交底书目录',
    disclosure_search: '搜索交底书内容',
    disclosure_read_section: '读取交底书章节',
    disclosure_edit: '编辑交底书',
    figure_kit: '管理交底书附图',
    file_glob: '查找文件',
    file_search: '搜索文件内容',
    file_read: '读取文件片段',
    exec_command: '执行本地或网络检索命令',
  };
  return descriptions[toolName] ?? '执行工具调用';
}

function formatToolScope(item: ToolCallEvent): string {
  return item.tool ?? item.title;
}
