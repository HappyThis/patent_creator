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
  streamSession: (
    project_id: string,
    session_id: string,
    onEvent: (event: string, payload: Record<string, unknown>) => void,
  ) => Promise<ChatStreamHandle>;
};

export const sseClient: SseClient = {
  async streamChatMessage(project_id, payload, onEvent) {
    return openSseStream(
      `${API_BASE_URL}/api/projects/${project_id}/chat/messages`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      },
      onEvent,
    );
  },

  async streamSession(project_id, session_id, onEvent) {
    return openSseStream(
      `${API_BASE_URL}/api/projects/${project_id}/sessions/${session_id}/stream`,
      {
        method: 'GET',
      },
      onEvent,
    );
  },
};

async function openSseStream(
  url: string,
  init: RequestInit,
  onEvent: (event: string, payload: Record<string, unknown>) => void,
): Promise<ChatStreamHandle> {
  const controller = new AbortController();
  const response = await fetch(url, {
    ...init,
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
    const emitBufferedEvents = (flush = false) => {
      while (true) {
        const boundary = findSseEventBoundary(buffer);
        if (!boundary) {
          break;
        }
        const chunk = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary.length);
        const parsed = parseSseChunk(chunk);
        if (!parsed) {
          continue;
        }
        onEvent(parsed.event, parsed.payload);
      }

      if (flush && buffer.trim()) {
        const parsed = parseSseChunk(buffer);
        buffer = '';
        if (parsed) {
          onEvent(parsed.event, parsed.payload);
        }
      }
    };

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          buffer += decoder.decode();
          emitBufferedEvents(true);
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        emitBufferedEvents();
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
}

function findSseEventBoundary(buffer: string): { index: number; length: number } | null {
  const boundaries = [
    { index: buffer.indexOf('\r\n\r\n'), length: 4 },
    { index: buffer.indexOf('\n\n'), length: 2 },
    { index: buffer.indexOf('\r\r'), length: 2 },
  ].filter((boundary) => boundary.index !== -1);

  if (boundaries.length === 0) {
    return null;
  }
  return boundaries.reduce((earliest, boundary) =>
    boundary.index < earliest.index ? boundary : earliest,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function parseSseChunk(chunk: string): { event: string; payload: Record<string, unknown> } | null {
  const lines = chunk.split(/\r\n|\n|\r/);
  let eventName = '';
  const dataLines: string[] = [];

  for (const line of lines) {
    if (!line || line.startsWith(':')) {
      continue;
    }
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim();
      continue;
    }
    if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  if (!eventName || dataLines.length === 0) {
    return null;
  }

  let payload: unknown;
  try {
    payload = JSON.parse(dataLines.join('\n'));
  } catch {
    return null;
  }
  if (!isRecord(payload)) {
    return null;
  }

  return {
    event: eventName,
    payload,
  };
}
