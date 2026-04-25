import { ChatMessage, TimelineItem } from '../../types';
import { TimelineList } from '../timeline/TimelineList';

type ChatPanelProps = {
  messages: ChatMessage[];
  timeline: TimelineItem[];
  composer: string;
  isBusy: boolean;
  onComposerChange: (value: string) => void;
  onSubmit: () => void;
};

export function ChatPanel({
  messages,
  timeline,
  composer,
  isBusy,
  onComposerChange,
  onSubmit,
}: ChatPanelProps) {
  return (
    <aside className="panel chat-panel">
      <div className="panel-header">
        <h2>Agent Chat</h2>
        <span>{isBusy ? 'running' : 'idle'}</span>
      </div>

      <div className="session-strip">
        <button className="session-pill active">sess_004</button>
        <button className="session-pill">sess_003</button>
        <button className="session-pill">sess_002</button>
      </div>

      <div className="chat-scroll">
        <section className="chat-thread">
          {messages.map((message) => (
            <article key={message.id} className={`message ${message.role}`}>
              <header>
                <strong>{message.role === 'user' ? '用户' : '主 agent'}</strong>
                <time>{message.timestamp}</time>
              </header>
              <p>{message.text}</p>
            </article>
          ))}
        </section>

        <TimelineList items={timeline} />
      </div>

      <div className="composer">
        <textarea
          value={composer}
          onChange={(event) => onComposerChange(event.target.value)}
          placeholder="输入本轮需求，当前原型会模拟一轮 SSE 与 document_edit 落盘。"
          disabled={isBusy}
        />
        <div className="composer-footer">
          <div className="references-note">
            支持 text / url / file_path 引用；当前原型只展示前端流程。
          </div>
          <button onClick={onSubmit} disabled={isBusy || composer.trim().length === 0}>
            {isBusy ? '处理中…' : '发送并播放示例回合'}
          </button>
        </div>
      </div>
    </aside>
  );
}
