"use client";

import React from 'react';

export default function ApprovalsPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between border-b border-gray-200 pb-4">
        <h1 className="text-2xl font-semibold text-gray-800">Approval Center</h1>
      </div>
      <div className="bg-white rounded shadow-sm p-6 text-center text-gray-500">
        <p>Pending Actions and Human-in-the-loop interactions will be rendered here.</p>
      </div>
    </div>
  );
}
