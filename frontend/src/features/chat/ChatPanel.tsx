import { UIEvent, useCallback, useLayoutEffect, useRef } from 'react';
import type { ChatEvent, ContextUsageSummary, QualityMode } from '../../types';
import { ChatComposer } from './ChatComposer';
import { ChatThread } from './ChatThread';

const AUTO_SCROLL_BOTTOM_THRESHOLD = 96;

type ChatPanelProps = {
  events: ChatEvent[];
  composer: string;
  isBusy: boolean;
  contextUsage?: ContextUsageSummary | null;
  qualityMode: QualityMode;
  onComposerChange: (value: string) => void;
  onQualityModeChange: (mode: QualityMode) => void;
  onSubmit: () => void;
  onCancel: () => void;
  canCancel?: boolean;
  isCancelling?: boolean;
};

export function ChatPanel({
  events,
  composer,
  isBusy,
  contextUsage,
  qualityMode,
  onComposerChange,
  onQualityModeChange,
  onSubmit,
  onCancel,
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
        composer={composer}
        isBusy={isBusy}
        contextUsage={contextUsage}
        qualityMode={qualityMode}
        canCancel={canCancel}
        isCancelling={isCancelling}
        onComposerChange={onComposerChange}
        onQualityModeChange={onQualityModeChange}
        onSubmit={onSubmit}
        onCancel={onCancel}
      />
    </aside>
  );
}
