import { useEffect, useRef, useState } from 'react';
import type { SessionTab } from '../../types';

type ChatHeaderProps = {
  sessionTabs: SessionTab[];
  isBusy: boolean;
  onExport: () => void;
  onSessionSelect: (session_id: string) => void;
  onNewSession: () => void;
};

export function ChatHeader({
  sessionTabs,
  isBusy,
  onExport,
  onSessionSelect,
  onNewSession,
}: ChatHeaderProps) {
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

  return (
    <div className="chat-header">
      <div className="chat-header-top" ref={sessionSwitcherRef}>
        <div className="chat-header-actions">
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
  );
}
