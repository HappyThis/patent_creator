import { useEffect, useRef } from 'react';
import type { ChatEvent, ContextUsageSummary, SessionTab } from '../../types';
import { ChatComposer } from './ChatComposer';
import { ChatHeader } from './ChatHeader';
import { ChatThread } from './ChatThread';

type ChatPanelProps = {
  sessionTabs: SessionTab[];
  events: ChatEvent[];
  composer: string;
  isBusy: boolean;
  contextUsage?: ContextUsageSummary | null;
  onComposerChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  onExport: () => void;
  onSessionSelect: (session_id: string) => void;
  onNewSession: () => void;
  canCancel?: boolean;
  isCancelling?: boolean;
};

export function ChatPanel({
  sessionTabs,
  events,
  composer,
  isBusy,
  contextUsage,
  onComposerChange,
  onSubmit,
  onCancel,
  onExport,
  onSessionSelect,
  onNewSession,
  canCancel = false,
  isCancelling = false,
}: ChatPanelProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = scrollRef.current;
    if (!container) return;
    container.scrollTop = container.scrollHeight;
  }, [events, sessionTabs]);

  return (
    <aside className="chat-panel">
      <ChatHeader
        sessionTabs={sessionTabs}
        isBusy={isBusy}
        onExport={onExport}
        onSessionSelect={onSessionSelect}
        onNewSession={onNewSession}
      />

      <div className="chat-scroll" ref={scrollRef}>
        <ChatThread events={events} />
      </div>

      <ChatComposer
        composer={composer}
        isBusy={isBusy}
        contextUsage={contextUsage}
        canCancel={canCancel}
        isCancelling={isCancelling}
        onComposerChange={onComposerChange}
        onSubmit={onSubmit}
        onCancel={onCancel}
      />
    </aside>
  );
}
