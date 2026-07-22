import { useAuth } from "@clerk/nextjs";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * A wrapper around fetch that automatically injects the Clerk JWT token.
 * This is meant to be used inside React components or hooks where `useAuth` is available.
 */
export const useApiClient = () => {
  const { getToken } = useAuth();

  const request = async <T>(endpoint: string, options: RequestInit = {}): Promise<T> => {
    const token = await getToken();
    
    const headers = {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
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
