'use client';

import { RefreshCw } from 'lucide-react';

export default function Error({
  reset,
}: {
  error: Error;
  reset: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-[#f4f6f9] text-gray-800 p-4">
      <div className="bg-white p-8 rounded-lg shadow-md max-w-md w-full text-center space-y-4 border border-gray-200">
        <h2 className="text-2xl font-bold text-gray-900">Something went wrong!</h2>
        <p className="text-xs text-gray-500 font-mono bg-gray-50 p-3 rounded border text-left overflow-x-auto">
          An unexpected client error occurred.
        </p>
        <button
          onClick={reset}
          className="bg-[#3c8dbc] hover:bg-[#367fa9] text-white px-4 py-2 rounded text-sm font-semibold transition-colors shadow-sm flex items-center justify-center mx-auto"
        >
          <RefreshCw className="w-4 h-4 mr-2" />
          Try Again
        </button>
      </div>
    </div>
  );
}
