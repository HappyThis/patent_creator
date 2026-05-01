export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000';

type ApiErrorResponse = {
  error?: {
    code?: string;
    message?: string;
  };
};

export async function readApiErrorMessage(response: Response): Promise<string> {
  let message = `Request failed with status ${response.status}`;

  try {
    const payload = (await response.json()) as ApiErrorResponse;
    if (payload.error?.message) {
      message = payload.error.message;
    }
  } catch {
    // Keep the status-based fallback when the backend does not return JSON.
  }

  return message;
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    throw new Error(await readApiErrorMessage(response));
  }

  return (await response.json()) as T;
}
