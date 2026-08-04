'use client';

/**
 * CRM session.
 *
 * The token lives in memory plus sessionStorage, so a page reload keeps the
 * operator signed in but closing the tab does not leave a credential behind.
 * Deliberately not localStorage: this is a bearer token for an API that can
 * authorize outbound email.
 *
 * There is no fallback identity. If sign-in fails, the CRM shows the sign-in
 * form -- it never proceeds with a guessed workspace, which is how the
 * pre-0.2 dashboard ended up rendering data that belonged to nobody.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { api, login, TitanError, type Principal, type Workspace } from '@/lib/titan';

const STORAGE_KEY = 'titan.session';

interface StoredSession {
  token: string;
  email: string;
  slug: string;
}

interface SessionValue {
  token: string | null;
  principal: Principal | null;
  workspace: Workspace | null;
  status: 'loading' | 'signed-out' | 'signed-in';
  error: string | null;
  signIn: (email: string, slug: string) => Promise<void>;
  signOut: () => void;
  /** True when the signed-in role holds every capability listed. */
  can: (...capabilities: string[]) => boolean;
}

const SessionContext = createContext<SessionValue | null>(null);

function read(): StoredSession | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as StoredSession) : null;
  } catch {
    return null;
  }
}

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  // Always 'loading' on the first render. sessionStorage does not exist during
  // server rendering, so deriving the initial value from it makes the server
  // and client disagree and React discards the tree as a hydration mismatch.
  const [status, setStatus] = useState<SessionValue['status']>('loading');
  const [error, setError] = useState<string | null>(null);

  const adopt = useCallback(async (next: StoredSession) => {
    // Fetch the principal before trusting the token: a stored token whose
    // membership was revoked must not present a signed-in shell.
    const [me, ws] = await Promise.all([api.me(next.token), api.workspace(next.token)]);
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    setToken(next.token);
    setPrincipal(me);
    setWorkspace(ws);
    setStatus('signed-in');
    setError(null);
  }, []);

  // Resolve the initial status. Both outcomes are reached from a settled
  // promise rather than from the effect body, so the first render is identical
  // on server and client and a revoked token never produces a signed-in shell.
  useEffect(() => {
    const stored = read();
    let cancelled = false;
    const restore = stored
      ? Promise.all([api.me(stored.token), api.workspace(stored.token)])
      : Promise.reject(new Error('no stored session'));

    restore
      .then(([me, ws]) => {
        if (cancelled || !stored) return;
        setToken(stored.token);
        setPrincipal(me);
        setWorkspace(ws);
        setStatus('signed-in');
      })
      .catch(() => {
        if (cancelled) return;
        window.sessionStorage.removeItem(STORAGE_KEY);
        setStatus('signed-out');
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = useCallback(
    async (email: string, slug: string) => {
      setError(null);
      try {
        const next = await login(email, slug);
        await adopt({ token: next, email, slug });
      } catch (e) {
        const message =
          e instanceof TitanError && e.status === 401
            ? 'No membership matches that email and workspace.'
            : e instanceof Error
              ? e.message
              : 'sign-in failed';
        setError(message);
        setStatus('signed-out');
        throw e;
      }
    },
    [adopt],
  );

  const signOut = useCallback(() => {
    window.sessionStorage.removeItem(STORAGE_KEY);
    setToken(null);
    setPrincipal(null);
    setWorkspace(null);
    setStatus('signed-out');
  }, []);

  const can = useCallback(
    (...capabilities: string[]) =>
      principal !== null && capabilities.every((c) => principal.capabilities.includes(c)),
    [principal],
  );

  const value = useMemo(
    () => ({ token, principal, workspace, status, error, signIn, signOut, can }),
    [token, principal, workspace, status, error, signIn, signOut, can],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (value === null) throw new Error('useSession must be used inside <SessionProvider>');
  return value;
}

/**
 * Load data for the signed-in session.
 *
 * Returns `pending` until a token exists, so a component never fires a request
 * with `undefined` as its bearer and misreads the resulting 401 as a real
 * authorization failure.
 */
export function useApi<T>(
  loader: (token: string) => Promise<T>,
  deps: React.DependencyList,
): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const { token } = useSession();
  const [nonce, setNonce] = useState(0);

  // What the request is *for*, so a settled result can be matched against the
  // request that is current. Loading is then derived rather than tracked, which
  // keeps every setState inside an async callback: a stale response arriving
  // after the filters changed can no longer overwrite the newer one.
  const key = [token ? 'auth' : 'anon', nonce, ...deps.map(String)].join('|');

  const [settled, setSettled] = useState<{
    key: string;
    data: T | null;
    error: string | null;
  }>({ key: '', data: null, error: null });

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    loader(token)
      .then((result) => {
        if (!cancelled) setSettled({ key, data: result, error: null });
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setSettled({
            key,
            data: null,
            error: e instanceof Error ? e.message : 'request failed',
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // `key` already encodes token, nonce, and every caller-supplied dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const current = settled.key === key;
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return {
    // Stale data stays visible while a refetch is in flight; a stale *error*
    // does not, because it may already have been resolved.
    data: settled.data,
    error: current ? settled.error : null,
    loading: token !== null && !current,
    reload,
  };
}
