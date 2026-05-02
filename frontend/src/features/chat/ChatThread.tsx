import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ChatEvent, ChatMessageEvent, ProcessEvent } from '../../types';
import { TimelineList } from '../timeline/TimelineList';

type ChatThreadProps = {
  events: ChatEvent[];
};

type RenderBlock =
  | { kind: 'message'; event: ChatMessageEvent }
  | { kind: 'process'; items: ProcessEvent[] }
  | { kind: 'round_status'; event: Extract<ChatEvent, { kind: 'round_status' }> };

export function ChatThread({ events }: ChatThreadProps) {
  const blocks = buildRenderBlocks(events);

  return (
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
  );
}

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
