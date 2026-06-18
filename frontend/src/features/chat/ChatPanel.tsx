import { UIEvent, useCallback, useLayoutEffect, useRef } from 'react';
import type { ChatEvent, ContextUsageSummary, SessionTab } from '../../types';
import { ChatComposer } from './ChatComposer';
import { ChatThread } from './ChatThread';

const AUTO_SCROLL_BOTTOM_THRESHOLD = 96;

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
  const shouldStickToBottomRef = useRef(true);

  const updateStickToBottom = useCallback((container: HTMLDivElement) => {
    const distanceToBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    shouldStickToBottomRef.current = distanceToBottom <= AUTO_SCROLL_BOTTOM_THRESHOLD;
  }, []);

  const handleScroll = useCallback(
    (event: UIEvent<HTMLDivElement>) => {
      updateStickToBottom(event.currentTarget);
    },
    [updateStickToBottom],
  );

  useLayoutEffect(() => {
    const container = scrollRef.current;
    if (!container) return;
    if (events.length === 0) {
      shouldStickToBottomRef.current = true;
      return;
    }
    if (shouldStickToBottomRef.current) {
      container.scrollTop = container.scrollHeight;
      updateStickToBottom(container);
    }
  }, [events, updateStickToBottom]);

  return (
    <aside className="chat-panel">
      <div className="chat-scroll" ref={scrollRef} onScroll={handleScroll}>
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
