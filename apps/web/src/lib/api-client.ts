const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * Legacy fetch wrapper for the pre-0.2 demo dashboard. Used only by
 * `components/operations/TaskDetailsClient.tsx` under `/dashboard`.
 *
 * It sends **no Authorization header** — an earlier comment here claimed it
 * "handles JWT token injection", which it has never done. Against the real
 * `/api/v1` every call would be a 401, which is correct: this client predates
 * authentication and must not be used for new work.
 *
 * The supported client is `lib/titan.ts`, which is typed per endpoint, takes an
 * explicit bearer token, and renders an error state rather than falling back to
 * sample data. New CRM code belongs there.
 */
export const useApiClient = () => {
  const request = async <T>(endpoint: string, options: RequestInit = {}): Promise<T> => {
    const headers = {
      'Content-Type': 'application/json',
      ...options.headers,
    };

    const response = await fetch(`${BASE_URL}${endpoint}`, {
      ...options,
      headers,
    });

    if (!response.ok) {
      throw new ApiError(response.status, `API request failed: ${response.statusText}`);
    }

    return response.json();
  };

  return {
    get: <T>(endpoint: string, options?: RequestInit) => request<T>(endpoint, { ...options, method: 'GET' }),
    post: <T>(endpoint: string, body: unknown, options?: RequestInit) => request<T>(endpoint, { ...options, method: 'POST', body: JSON.stringify(body) }),
    put: <T>(endpoint: string, body: unknown, options?: RequestInit) => request<T>(endpoint, { ...options, method: 'PUT', body: JSON.stringify(body) }),
    delete: <T>(endpoint: string, options?: RequestInit) => request<T>(endpoint, { ...options, method: 'DELETE' }),
  };
};
