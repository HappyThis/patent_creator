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
  const subagentParentById = new Map<string, ToolNode>();
  const roots: ToolNode[] = [];

  for (const item of toolCalls) {
    const node = { item, children: [] };
    nodeById.set(item.id, node);
    const launchedSubagentId = getLaunchedSubagentId(item);
    if (launchedSubagentId) {
      subagentParentById.set(launchedSubagentId, node);
    }
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
      continue;
    }

    const subagentId = getScopeSubagentId(item);
    const subagentParent = subagentId ? subagentParentById.get(subagentId) : undefined;
    if (subagentParent && subagentParent !== node) {
      subagentParent.children.push(node);
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
    disclosure_outline: '读取交底书目录',
    disclosure_search: '搜索交底书内容',
    disclosure_read_section: '读取交底书章节',
    disclosure_edit: '编辑交底书',
    execute_subagent: '调度专业子任务',
    exec_command: '执行本地或网络检索命令',
    analysis: '结构化关键信息',
    section_writer: '生成章节草稿',
  };
  return descriptions[toolName] ?? '执行工具调用';
}

function formatToolScope(item: ToolCallEvent): string {
  if (item.scope?.startsWith('subagent:')) {
    return `${item.scope.replace('subagent:', '')} / ${item.tool ?? item.title}`;
  }
  return item.tool ?? item.title;
}

function getScopeSubagentId(item: ToolCallEvent): string | null {
  if (!item.scope?.startsWith('subagent:')) {
    return null;
  }
  return item.scope.replace('subagent:', '') || null;
}

function getLaunchedSubagentId(item: ToolCallEvent): string | null {
  if (item.tool !== 'execute_subagent') {
    return null;
  }
  const detail = parseToolDetail(item.detail);
  return findAgentId(detail);
}

function parseToolDetail(detail?: string): unknown {
  if (!detail) {
    return null;
  }
  try {
    return JSON.parse(detail);
  } catch {
    return null;
  }
}

function findAgentId(value: unknown): string | null {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (typeof record.agent_id === 'string') {
    return record.agent_id;
  }
  return findAgentId(record.arguments) ?? findAgentId(record.output) ?? findAgentId(record.result);
}
