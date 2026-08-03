import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  images: {
    unoptimized: true,
  },
  // `ignoreBuildErrors: true` was removed in the 0.2 hardening pass (gap
  // analysis C-12). It shipped the dashboard with unknown type errors and left
  // the CI type check as the only gate. `tsc --noEmit` currently exits 0, so
  // there was nothing being suppressed -- the flag was pure risk.
  typescript: {
    ignoreBuildErrors: false,
  },
};

export default nextConfig;
