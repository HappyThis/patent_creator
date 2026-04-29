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
      className={`command-item ${item.status ?? 'plain'} ${hasChildren ? 'has-children' : ''}`}
      open={item.status === 'running'}
    >
      <summary className="command-item-summary">
        <span className={`status-dot ${item.status ?? 'plain'}`} />
        <span>{item.tool ?? item.title}</span>
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

function formatToolScope(item: ToolCallEvent): string {
  if (item.scope?.startsWith('subagent:')) {
    return `${item.scope.replace('subagent:', '')} / ${item.tool ?? item.title}`;
  }
  return item.tool ?? item.title;
}
