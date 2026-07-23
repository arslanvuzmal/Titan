import Link from 'next/link';
import { AlertCircle } from 'lucide-react';

export default function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-[#f4f6f9] text-gray-800 p-4">
      <div className="bg-white p-8 rounded-lg shadow-md max-w-md w-full text-center space-y-4 border border-gray-200">
        <div className="p-3 bg-red-50 text-red-500 rounded-full w-12 h-12 mx-auto flex items-center justify-center">
          <AlertCircle className="w-6 h-6" />
        </div>
        <h1 className="text-3xl font-bold text-gray-900">404 - Page Not Found</h1>
        <p className="text-sm text-gray-500">
          The page or route you are looking for does not exist in TITAN OS.
        </p>
        <div className="pt-2">
          <Link 
            href="/dashboard" 
            className="inline-block bg-[#3c8dbc] hover:bg-[#367fa9] text-white px-5 py-2 rounded text-sm font-semibold transition-colors shadow-sm"
          >
            Return to Command Center
          </Link>
        </div>
      </div>
    </div>
  );
}
