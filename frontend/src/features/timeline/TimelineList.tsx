import { ProcessEvent, ToolCallEvent } from '../../types';

type TimelineListProps = {
  items: ProcessEvent[];
};

type ToolNode = {
  item: ToolCallEvent;
  children: ToolNode[];
};

export function TimelineList({ items }: TimelineListProps) {
  const toolCalls = items.filter((item) => item.kind === 'tool_call');
  const toolTree = buildToolTree(toolCalls);

  return (
    <details className="process-group">
      <summary className="process-summary">
        <span>执行过程</span>
        <span className="process-summary-meta">{toolCalls.length} 个工具调用</span>
      </summary>

      <div className="process-body">
        <div className="command-group-body">
          {toolTree.map((node) => renderToolNode(node))}
        </div>
      </div>
    </details>
  );
}

function buildToolTree(toolCalls: ToolCallEvent[]): ToolNode[] {
  const nodeById = new Map<string, ToolNode>();
  const roots: ToolNode[] = [];

  for (const item of toolCalls) {
    nodeById.set(item.id, { item, children: [] });
  }

  for (const item of toolCalls) {
    const node = nodeById.get(item.id);
    if (!node) {
      continue;
    }
    const parentId = item.parent_call_id;
    const parent = parentId ? nodeById.get(parentId) : undefined;
    if (parent) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }

  return roots;
}

function renderToolNode(node: ToolNode) {
  const item = node.item;
  const hasChildren = node.children.length > 0;

  return (
    <details
      key={item.id}
      className={`command-item ${item.status ?? 'plain'} ${toolToneClass(item.tool ?? item.title)} ${hasChildren ? 'has-children' : ''}`}
      open={item.status === 'running'}
    >
      <summary className="command-item-summary">
        <span className={`status-dot ${item.status ?? 'plain'}`} />
        <span className="command-label">
          <span className="command-title">{item.tool ?? item.title}</span>
          <span className="command-description">{describeTool(item.tool ?? item.title)}</span>
        </span>
        {hasChildren ? <span className="command-child-count">{node.children.length} 个子调用</span> : null}
      </summary>
      {hasChildren ? <div className="command-children">{node.children.map((child) => renderToolNode(child))}</div> : null}
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
    document_read: '读取参考文档与章节内容',
    document_edit: '写回并优化交底书正文',
    execute_subagent: '调度专业子任务',
    exec_command: '执行本地或网络检索命令',
    analysis: '结构化关键信息',
    section_writer: '生成章节草稿',
  };
  return descriptions[toolName] ?? '执行工具调用';
}

function toolToneClass(toolName: string): string {
  const tones: Record<string, string> = {
    document_read: 'tool-tone-read',
    document_edit: 'tool-tone-edit',
    execute_subagent: 'tool-tone-agent',
    exec_command: 'tool-tone-search',
    web_search: 'tool-tone-search',
    analysis: 'tool-tone-analysis',
    section_writer: 'tool-tone-write',
  };
  return tones[toolName] ?? 'tool-tone-default';
}

function formatToolScope(item: ToolCallEvent): string {
  if (item.scope?.startsWith('subagent:')) {
    return `${item.scope.replace('subagent:', '')} / ${item.tool ?? item.title}`;
  }
  return item.tool ?? item.title;
}
