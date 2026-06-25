export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:5172';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function apiErrorMessage(payload: unknown): string | null {
  if (!isRecord(payload) || !isRecord(payload.error)) {
    return null;
  }
  return typeof payload.error.message === 'string' ? payload.error.message : null;
}

export async function readApiErrorMessage(response: Response): Promise<string> {
  let message = `Request failed with status ${response.status}`;

  try {
    const payload: unknown = await response.json();
    const parsedMessage = apiErrorMessage(payload);
    if (parsedMessage) {
      message = parsedMessage;
    }
  } catch {
    // Keep the status-based fallback when the backend does not return JSON.
  }

  return message;
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    throw new Error(await readApiErrorMessage(response));
  }

  return (await response.json()) as T;
}

export async function requestFile(path: string, init?: RequestInit): Promise<{ blob: Blob; filename: string | null }> {
  const headers = new Headers(init?.headers);
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    throw new Error(await readApiErrorMessage(response));
  }

  return {
    blob: await response.blob(),
    filename: filenameFromContentDisposition(response.headers.get('Content-Disposition')),
  };
}

function filenameFromContentDisposition(value: string | null): string | null {
  if (!value) {
    return null;
  }
  const encodedMatch = value.match(/filename\*=UTF-8''([^;]+)/i);
  if (encodedMatch?.[1]) {
    try {
      return decodeURIComponent(encodedMatch[1]);
    } catch {
      return encodedMatch[1];
    }
  }
  const plainMatch = value.match(/filename="?([^";]+)"?/i);
  return plainMatch?.[1] ?? null;
}
