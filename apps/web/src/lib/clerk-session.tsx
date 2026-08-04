'use client';

/**
 * Clerk-backed sessions, for a deployed Titan.
 *
 * The API refuses to mint its own tokens when TITAN_ENVIRONMENT=production, so
 * this is the only way to sign in to a deployment. It fills the same
 * `SessionContext` as the local provider, so every screen is unaware of which
 * mode it is running under.
 *
 * The token is fetched from Clerk on each render pass rather than stored.
 * Clerk session tokens are short-lived and it refreshes them itself; caching
 * one in sessionStorage would mean holding a credential past the point Clerk
 * considers it valid, for no benefit.
 *
 * As in the local path, the principal is fetched before the shell renders: a
 * Clerk identity with no Titan membership must land on a clear message, not on
 * a CRM whose every request then fails.
 */

import { ClerkProvider, SignIn, useAuth } from '@clerk/nextjs';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { CLERK_PUBLISHABLE_KEY } from '@/lib/authMode';
import { SessionContext, type SessionValue } from '@/lib/session';
import { api, type Principal, type Workspace } from '@/lib/titan';

function ClerkSessionBridge({ children }: { children: React.ReactNode }) {
  const { isLoaded, isSignedIn, getToken, signOut: clerkSignOut } = useAuth();

  const [token, setToken] = useState<string | null>(null);
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [status, setStatus] = useState<SessionValue['status']>('loading');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoaded) return;
    let cancelled = false;

    const resolve = async () => {
      if (!isSignedIn) throw new Error('not signed in');
      const next = await getToken();
      if (!next) throw new Error('Clerk returned no token');
      const [me, ws] = await Promise.all([api.me(next), api.workspace(next)]);
      return { next, me, ws };
    };

    resolve()
      .then(({ next, me, ws }) => {
        if (cancelled) return;
        setToken(next);
        setPrincipal(me);
        setWorkspace(ws);
        setStatus('signed-in');
        setError(null);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setStatus('signed-out');
        // A Clerk identity with no Titan membership is the expected first-run
        // failure, and the message says so rather than reading as a bug.
        setError(
          isSignedIn
            ? e instanceof Error
              ? `Signed in to Clerk, but this identity has no Titan access: ${e.message}`
              : 'this identity has no Titan access'
            : null,
        );
      });

    return () => {
      cancelled = true;
    };
  }, [isLoaded, isSignedIn, getToken]);

  const signIn = useCallback(async () => {
    // Clerk owns the sign-in flow; the shell never collects credentials.
    throw new Error('sign-in is handled by Clerk');
  }, []);

  const signOut = useCallback(() => {
    setToken(null);
    setPrincipal(null);
    setWorkspace(null);
    setStatus('signed-out');
    void clerkSignOut();
  }, [clerkSignOut]);

  const can = useCallback(
    (...capabilities: string[]) =>
      principal !== null && capabilities.every((c) => principal.capabilities.includes(c)),
    [principal],
  );

  const value = useMemo(
    () => ({ token, principal, workspace, status, error, signIn, signOut, can }),
    [token, principal, workspace, status, error, signIn, signOut, can],
  );

  // Clerk is loaded and nobody is signed in: show Clerk's own form rather than
  // the shell's, which would collect an email the API will not accept.
  if (isLoaded && !isSignedIn) {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-4 bg-slate-50 px-4">
        <h1 className="text-lg font-semibold text-slate-900">Titan-OS</h1>
        <SignIn routing="hash" />
      </main>
    );
  }

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function ClerkSessionProvider({ children }: { children: React.ReactNode }) {
  if (!CLERK_PUBLISHABLE_KEY) {
    // Reached only if the build-time flag and this key disagree. Saying so is
    // better than rendering a sign-in form that cannot work.
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
        <p className="max-w-md rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          Clerk auth is selected but NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY is
          not set, so there is no way to sign in. Set it on the deployment, or unset
          it to fall back to local development auth.
        </p>
      </main>
    );
  }

  return (
    <ClerkProvider publishableKey={CLERK_PUBLISHABLE_KEY}>
      <ClerkSessionBridge>{children}</ClerkSessionBridge>
    </ClerkProvider>
  );
}
