import { CompositionEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ChatEvent, ChatMessageEvent, ContextUsageSummary, ProcessEvent, SessionTab } from '../../types';
import { TimelineList } from '../timeline/TimelineList';

type ChatPanelProps = {
  sessionTabs: SessionTab[];
  events: ChatEvent[];
  composer: string;
  isBusy: boolean;
  contextUsage?: ContextUsageSummary | null;
  onComposerChange: (value: string) => void;
  onSubmit: () => void;
  onSessionSelect: (session_id: string) => void;
  onNewSession: () => void;
};

type RenderBlock =
  | { kind: 'message'; event: ChatMessageEvent }
  | { kind: 'process'; items: ProcessEvent[] }
  | { kind: 'round_status'; event: Extract<ChatEvent, { kind: 'round_status' }> };

function buildRenderBlocks(events: ChatEvent[]): RenderBlock[] {
  const blocks: RenderBlock[] = [];
  let pendingProcess: ProcessEvent[] = [];

  const flushProcess = () => {
    if (pendingProcess.length === 0) {
      return;
    }
    blocks.push({ kind: 'process', items: pendingProcess });
    pendingProcess = [];
  };

  for (const event of events) {
    if (event.kind === 'message') {
      flushProcess();
      blocks.push({ kind: 'message', event });
      continue;
    }
    if (event.kind === 'tool_call') {
      pendingProcess.push(event);
      continue;
    }
    if (event.kind === 'round_status') {
      flushProcess();
      blocks.push({ kind: 'round_status', event });
    }
  }
  flushProcess();

  return blocks;
}

export function ChatPanel({
  sessionTabs,
  events,
  composer,
  isBusy,
  contextUsage,
  onComposerChange,
  onSubmit,
  onSessionSelect,
  onNewSession,
}: ChatPanelProps) {
  const [isComposing, setIsComposing] = useState(false);
  const [isSessionMenuOpen, setIsSessionMenuOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sessionSwitcherRef = useRef<HTMLDivElement | null>(null);

  const handleCompositionStart = (_event: CompositionEvent<HTMLTextAreaElement>) => {
    setIsComposing(true);
  };

  const handleCompositionEnd = (_event: CompositionEvent<HTMLTextAreaElement>) => {
    setIsComposing(false);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey) {
      return;
    }
    // 输入法组合中，回车用于确认候选词，不应触发发送。
    // isComposing 覆盖绝大多数浏览器；event.nativeEvent.isComposing / keyCode===229 兜底。
    if (isComposing || event.nativeEvent.isComposing || event.keyCode === 229) {
      return;
    }
    event.preventDefault();
    onSubmit();
  };

  const blocks = buildRenderBlocks(events);

  const handleSessionSelect = (sessionId: string) => {
    setIsSessionMenuOpen(false);
    onSessionSelect(sessionId);
  };

  const handleNewSession = () => {
    setIsSessionMenuOpen(false);
    onNewSession();
  };

  useEffect(() => {
    // 每次事件更新后滚动到底部，确保最新消息可见。
    const container = scrollRef.current;
    if (!container) return;
    container.scrollTop = container.scrollHeight;
  }, [events, sessionTabs]);

  useEffect(() => {
    if (!isSessionMenuOpen) {
      return undefined;
    }

    const handlePointerDown = (event: MouseEvent) => {
      if (!sessionSwitcherRef.current?.contains(event.target as Node)) {
        setIsSessionMenuOpen(false);
      }
    };
    const handleEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsSessionMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [isSessionMenuOpen]);

  const contextPercent = contextUsage ? Math.round(contextUsage.used_ratio * 100) : 0;
  const contextBarWidth = Math.min(100, Math.max(0, contextPercent));

  return (
    <aside className="chat-panel">
      <div className="chat-header">
        <div className="chat-header-top" ref={sessionSwitcherRef}>
          <div className="chat-agent-heading">
            <span className="chat-agent-title">AI 协作助手</span>
            <span className={`chat-agent-status ${isBusy ? 'running' : 'idle'}`}>
              <span className="chat-agent-status-dot" aria-hidden="true" />
              {isBusy ? '运行中' : '就绪'}
            </span>
          </div>
          <div className="chat-header-actions">
            <button
              className="session-history-button"
              type="button"
              onClick={() => setIsSessionMenuOpen((current) => !current)}
              aria-expanded={isSessionMenuOpen}
              aria-haspopup="menu"
              aria-label="历史会话"
              title="历史会话"
            >
              <svg className="session-history-icon" viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="7" />
                <path d="M12 8.2v4.05l2.75 1.65" />
              </svg>
            </button>
            <button
              className="session-new-button"
              type="button"
              onClick={handleNewSession}
              disabled={isBusy}
              aria-label="新建会话"
              title="新建会话"
            >
              <svg className="session-new-icon" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 5v14" />
                <path d="M5 12h14" />
              </svg>
            </button>
          </div>

          {isSessionMenuOpen ? (
            <div className="session-menu" role="menu">
              {sessionTabs.length > 0 ? (
                sessionTabs.map((tab) => (
                  <button
                    key={tab.session_id}
                    className={`session-menu-item ${tab.active ? 'active' : ''}`}
                    type="button"
                    role="menuitem"
                    onClick={() => handleSessionSelect(tab.session_id)}
                    disabled={isBusy && !tab.active}
                  >
                    <span className="session-menu-title">{tab.title}</span>
                    {tab.subtitle ? <span className="session-menu-subtitle">{tab.subtitle}</span> : null}
                  </button>
                ))
              ) : (
                <div className="session-menu-empty">暂无历史会话</div>
              )}
            </div>
          ) : null}
        </div>
      </div>

      <div className="chat-scroll" ref={scrollRef}>
        <section className="chat-thread">
          {blocks.map((block, index) => {
            if (block.kind === 'message') {
              const { event } = block;
              return (
                <article key={event.id} className={`message-row ${event.role}`}>
                  <div className={`message-bubble ${event.role}`}>
                    <div className="markdown-body">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{
                          a: ({ node: _node, ...props }) => (
                            <a {...props} target="_blank" rel="noopener noreferrer" />
                          ),
                        }}
                      >
                        {event.text ?? ''}
                      </ReactMarkdown>
                    </div>
                  </div>
                  <time>{event.timestamp}</time>
                </article>
              );
            }

            if (block.kind === 'process') {
              return <TimelineList key={`process_${index}`} items={block.items} />;
            }

            return (
              <article key={block.event.id} className={`round-status ${block.event.status}`}>
                <span>{block.event.summary}</span>
                {block.event.detail ? <small>{block.event.detail}</small> : null}
              </article>
            );
          })}
        </section>
      </div>

      <div className="composer">
        <div className="composer-inline">
          <textarea
            value={composer}
            onChange={(event) => onComposerChange(event.target.value)}
            onKeyDown={handleKeyDown}
            onCompositionStart={handleCompositionStart}
            onCompositionEnd={handleCompositionEnd}
            placeholder="描述你的发明想法，或说明要补充的章节"
            rows={3}
            disabled={isBusy}
          />
          <div className="composer-toolbar">
            <div className="composer-toolbar-left" />
            <div className="composer-toolbar-right">
              {contextUsage ? (
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
                    <p>
                      {contextUsage.status === 'over_limit'
                        ? '接近上限，系统将压缩早期上下文'
                        : '接近上限时会自动压缩'}
                    </p>
                  </div>
                </div>
              ) : null}
              <button
                className="composer-send-inline"
                onClick={onSubmit}
                disabled={isBusy || composer.trim().length === 0}
                aria-label="发送"
                title="发送"
              >
                ↑
              </button>
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
}

function formatCompactTokens(value: number): string {
  if (value >= 1000) {
    return `${Math.round(value / 1000)}k`;
  }
  return String(value);
}
