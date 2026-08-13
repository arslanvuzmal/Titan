import { redirect } from 'next/navigation';

/**
 * The root sent visitors to the Next.js starter template ("To get started,
 * edit the page.tsx file"), which is what the deployed site served at `/`.
 *
 * There is one product here and it lives at /crm. A landing page would be a
 * second thing to keep true.
 */
export default function Home() {
  redirect('/crm');
}
