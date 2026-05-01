import { API_BASE_URL, readApiErrorMessage } from '../api/http';

export type ChatStreamPayload = {
  session_id?: string | null;
  message: string;
  active_section_id?: string | null;
  active_block_id?: string | null;
};

export type ChatStreamHandle = {
  close: () => void;
  done: Promise<void>;
};

export type SseClient = {
  streamChatMessage: (
    project_id: string,
    payload: ChatStreamPayload,
    onEvent: (event: string, payload: Record<string, unknown>) => void,
  ) => Promise<ChatStreamHandle>;
};

export const sseClient: SseClient = {
  async streamChatMessage(project_id, payload, onEvent) {
    const controller = new AbortController();
    const response = await fetch(`${API_BASE_URL}/api/projects/${project_id}/chat/messages`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(await readApiErrorMessage(response));
    }

    if (!response.body) {
      throw new Error('流式响应缺少 body。');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    const done = (async () => {
      let buffer = '';
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) {
            break;
          }
          buffer += decoder.decode(value, { stream: true });

          while (true) {
            const boundary = buffer.indexOf('\n\n');
            if (boundary === -1) {
              break;
            }
            const chunk = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const parsed = parseSseChunk(chunk);
            if (!parsed) {
              continue;
            }
            onEvent(parsed.event, parsed.payload);
          }
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') {
          return;
        }
        throw error;
      }
    })();

    return {
      close() {
        controller.abort();
      },
      done,
    };
  },
};

function parseSseChunk(chunk: string): { event: string; payload: Record<string, unknown> } | null {
  const lines = chunk.split('\n');
  let eventName = '';
  const dataLines: string[] = [];

  for (const line of lines) {
    if (!line || line.startsWith(':')) {
      continue;
    }
    if (line.startsWith('event: ')) {
      eventName = line.slice(7).trim();
      continue;
    }
    if (line.startsWith('data: ')) {
      dataLines.push(line.slice(6));
    }
  }

  if (!eventName || dataLines.length === 0) {
    return null;
  }

  return {
    event: eventName,
    payload: JSON.parse(dataLines.join('\n')) as Record<string, unknown>,
  };
}
