'use client';

/**
 * The CRM shell: sign-in gate, navigation, and the mode banner.
 *
 * The banner is not decoration. Titan refuses to send unless the process kill
 * switch and the workspace authorization are both on, and an operator working
 * a lead needs to know which of those is holding delivery -- otherwise
 * "approved" looks like "sent" and the queue silently accumulates.
 */

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import React, { useState } from 'react';
import { useSession } from '@/lib/session';
import { api, type SendingPreflight } from '@/lib/titan';
import { Badge, Button, Spinner } from './ui';

const NAV = [
  { href: '/crm', label: 'Overview', exact: true },
  { href: '/crm/leads', label: 'Leads' },
  { href: '/crm/approvals', label: 'Approvals' },
  { href: '/crm/campaigns', label: 'Campaigns' },
  { href: '/crm/messages', label: 'Delivery' },
  { href: '/crm/compliance', label: 'Compliance' },
  { href: '/crm/operations', label: 'Operations' },
];

/**
 * Two fields. The workspace is resolved from the account's membership rather
 * than typed, because it was never a credential -- asking for it made the form
 * look like it checked three things when it checked none.
 */
function SignIn() {
  const { signIn, error } = useSession();
  const [username, setUsername] = useState('');
  const [passcode, setPasscode] = useState('');
  const [busy, setBusy] = useState(false);

  const field =
    'mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 ' +
    'normal-case tracking-normal outline-none focus:border-slate-500';

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            await signIn(username.trim(), passcode);
          } catch {
            /* the message is rendered from session state */
          } finally {
            setBusy(false);
          }
        }}
        className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-6 shadow-sm"
      >
        <h1 className="text-lg font-semibold text-slate-900">Titan-OS</h1>
        <p className="mt-1 text-sm text-slate-500">Sign in to continue.</p>

        <label className="mt-5 block text-xs font-medium uppercase tracking-wide text-slate-500">
          Username
          <input
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            className={field}
          />
        </label>

        <label className="mt-3 block text-xs font-medium uppercase tracking-wide text-slate-500">
          Passcode
          {/* Not trimmed on submit: a passcode that legitimately ends in a
              space would silently stop working. `titan set-passcode` refuses
              to create one, so the two ends agree. */}
          <input
            type="password"
            required
            value={passcode}
            onChange={(e) => setPasscode(e.target.value)}
            autoComplete="current-password"
            className={field}
          />
        </label>

        {error && (
          <p className="mt-3 rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700">{error}</p>
        )}

        <div className="mt-5">
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </Button>
        </div>

        <p className="mt-4 text-xs text-slate-400">
          Five failed attempts locks the account for fifteen minutes. There is no
          self-service reset — an operator runs <code>titan set-passcode</code>.
        </p>
      </form>
    </main>
  );
}

function ModeBanner({ preflight }: { preflight: SendingPreflight | null }) {
  const { workspace } = useSession();
  if (!workspace) return null;

  const blocked = preflight ? !preflight.would_send : true;
  return (
    <div
      className={`border-b px-6 py-2 text-xs ${
        blocked
          ? 'border-amber-200 bg-amber-50 text-amber-900'
          : 'border-emerald-200 bg-emerald-50 text-emerald-900'
      }`}
    >
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-4 gap-y-1">
        <span className="font-semibold">
          {blocked ? 'Delivery is blocked' : 'Delivery is live'}
        </span>
        <span>
          mode <code className="font-mono">{workspace.operating_mode}</code>
        </span>
        <span>
          provider <code className="font-mono">{preflight?.email_provider ?? 'unknown'}</code>
        </span>
        {preflight && preflight.blockers.length > 0 && (
          <span className="truncate">blocked by: {preflight.blockers.join('; ')}</span>
        )}
      </div>
    </div>
  );
}

export function Shell({ children }: { children: React.ReactNode }) {
  const { status, principal, workspace, signOut, token } = useSession();
  const pathname = usePathname();
  const [preflight, setPreflight] = useState<SendingPreflight | null>(null);

  React.useEffect(() => {
    if (!token) return;
    api.preflight().then(setPreflight).catch(() => setPreflight(null));
  }, [token]);

  if (status === 'loading') {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-50">
        <Spinner label="Restoring session" />
      </main>
    );
  }
  if (status === 'signed-out') return <SignIn />;

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
          <Link href="/crm" className="text-sm font-semibold text-slate-900">
            Titan-OS
          </Link>
          <nav className="flex flex-1 flex-wrap items-center gap-1">
            {NAV.map((item) => {
              const active = item.exact
                ? pathname === item.href
                : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`rounded-lg px-3 py-1.5 text-sm transition ${
                    active
                      ? 'bg-slate-900 text-white'
                      : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
          <div className="flex items-center gap-3 text-right">
            <div className="hidden sm:block">
              <p className="text-xs font-medium text-slate-900">{principal?.email}</p>
              <p className="text-[11px] text-slate-500">
                {workspace?.name} · <Badge>{principal?.role ?? ''}</Badge>
              </p>
            </div>
            <Button variant="ghost" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </div>
      </header>

      <ModeBanner preflight={preflight} />

      <main className="mx-auto max-w-7xl px-6 py-6">{children}</main>
    </div>
  );
}
