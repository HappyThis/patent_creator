import { useEffect, useRef } from 'react';
import type { ChatEvent, ContextUsageSummary, SessionTab } from '../../types';
import { ChatComposer } from './ChatComposer';
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
  }, [events]);

  return (
    <aside className="chat-panel">
      <div className="chat-scroll" ref={scrollRef}>
        <ChatThread events={events} />
      </div>

      <ChatComposer
        sessionTabs={sessionTabs}
        composer={composer}
        isBusy={isBusy}
        contextUsage={contextUsage}
        canCancel={canCancel}
        isCancelling={isCancelling}
        onComposerChange={onComposerChange}
        onSubmit={onSubmit}
        onCancel={onCancel}
        onSessionSelect={onSessionSelect}
        onNewSession={onNewSession}
      />
    </aside>
  );
}
