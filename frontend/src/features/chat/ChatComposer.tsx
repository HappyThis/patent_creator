import { CompositionEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';
import type { ContextUsageSummary, SessionTab } from '../../types';
import { ContextUsageBadge } from './ContextUsageBadge';

type ChatComposerProps = {
  sessionTabs: SessionTab[];
  composer: string;
  isBusy: boolean;
  contextUsage?: ContextUsageSummary | null;
  canCancel?: boolean;
  isCancelling?: boolean;
  onComposerChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  onExport: () => void;
  onSessionSelect: (session_id: string) => void;
  onNewSession: () => void;
};

export function ChatComposer({
  sessionTabs,
  composer,
  isBusy,
  contextUsage,
  canCancel = false,
  isCancelling = false,
  onComposerChange,
  onSubmit,
  onCancel,
  onExport,
  onSessionSelect,
  onNewSession,
}: ChatComposerProps) {
  const [isComposing, setIsComposing] = useState(false);
  const [isSessionMenuOpen, setIsSessionMenuOpen] = useState(false);
  const sessionSwitcherRef = useRef<HTMLDivElement | null>(null);

  const handleSessionSelect = (sessionId: string) => {
    setIsSessionMenuOpen(false);
    onSessionSelect(sessionId);
  };

  const handleNewSession = () => {
    setIsSessionMenuOpen(false);
    onNewSession();
  };

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
    if (isComposing || event.nativeEvent.isComposing || event.keyCode === 229) {
      return;
    }
    event.preventDefault();
    onSubmit();
  };

  return (
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
          <div className="composer-toolbar-right">
            <ContextUsageBadge contextUsage={contextUsage} />
            <div className="chat-header-actions composer-session-actions" ref={sessionSwitcherRef}>
              <button
                className="session-export-button"
                type="button"
                onClick={onExport}
                disabled={isBusy}
                aria-label="导出 Markdown"
                title="导出 Markdown"
              >
                <svg className="session-export-icon" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M12 4v10" />
                  <path d="m8.5 10.5 3.5 3.5 3.5-3.5" />
                  <path d="M5 19h14" />
                </svg>
              </button>
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
            <button
              className={`composer-send-inline ${canCancel ? 'cancel' : ''}`}
              onClick={canCancel ? onCancel : onSubmit}
              disabled={canCancel ? isCancelling : isBusy || composer.trim().length === 0}
              aria-label={canCancel ? '取消任务' : '发送'}
              title={canCancel ? '取消任务' : '发送'}
            >
              {canCancel ? <span className="composer-stop-icon" aria-hidden="true" /> : '↑'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
