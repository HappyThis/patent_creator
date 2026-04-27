import { KeyboardEvent } from 'react';
import { ChatEvent, ChatMessageEvent, ProcessEvent, SessionTab } from '../../types';
import { TimelineList } from '../timeline/TimelineList';

type ChatPanelProps = {
  sessionTabs: SessionTab[];
  events: ChatEvent[];
  composer: string;
  isBusy: boolean;
  onComposerChange: (value: string) => void;
  onSubmit: () => void;
  onSessionSelect: (session_id: string) => void;
};

type RenderBlock =
  | { kind: 'message'; event: ChatMessageEvent }
  | { kind: 'process'; items: ProcessEvent[] };

function buildRenderBlocks(events: ChatEvent[]): RenderBlock[] {
  const blocks: RenderBlock[] = [];
  let processBuffer: ProcessEvent[] = [];

  const flushProcess = () => {
    if (processBuffer.length > 0) {
      blocks.push({ kind: 'process', items: processBuffer });
      processBuffer = [];
    }
  };

  for (const event of events) {
    if (event.kind === 'message') {
      flushProcess();
      blocks.push({ kind: 'message', event });
      continue;
    }

    processBuffer.push(event);
  }

  flushProcess();
  return blocks;
}

export function ChatPanel({
  sessionTabs,
  events,
  composer,
  isBusy,
  onComposerChange,
  onSubmit,
  onSessionSelect,
}: ChatPanelProps) {
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      onSubmit();
    }
  };

  const blocks = buildRenderBlocks(events);

  return (
    <aside className="chat-panel">
      <div className="chat-header">
        <div className="session-tab-strip" role="tablist" aria-label="Sessions">
          {sessionTabs.map((tab) => (
            <button
              key={tab.session_id}
              className={`session-card-tab ${tab.active ? 'active' : ''}`}
              role="tab"
              aria-selected={tab.active}
              onClick={() => onSessionSelect(tab.session_id)}
              disabled={isBusy && !tab.active}
            >
              <span className="session-card-title">{tab.title}</span>
              {tab.subtitle ? <span className="session-card-subtitle">{tab.subtitle}</span> : null}
            </button>
          ))}
        </div>
      </div>

      <div className="chat-scroll">
        <section className="chat-thread">
          {blocks.map((block, index) => {
            if (block.kind === 'message') {
              const { event } = block;
              return (
                <article key={event.id} className={`message-row ${event.role}`}>
                  <div className={`message-bubble ${event.role}`}>
                    <p>{event.text}</p>
                  </div>
                  <time>{event.timestamp}</time>
                </article>
              );
            }

            return <TimelineList key={`process_${index}`} items={block.items} />;
          })}
        </section>
      </div>

      <div className="composer">
        <div className="composer-inline">
          <textarea
            value={composer}
            onChange={(event) => onComposerChange(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="请输入需求"
            rows={3}
            disabled={isBusy}
          />
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
    </aside>
  );
}
