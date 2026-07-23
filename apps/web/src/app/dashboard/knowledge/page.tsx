"use client";

import React, { useState } from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { Book, Search, FileText, Upload, CheckCircle2, Database } from 'lucide-react';

const sampleDocs = [
  { id: 'doc-1', title: 'Enterprise Security Compliance Policy 2026', type: 'PDF', size: '2.4 MB', vectors: 1420, date: '2026-07-15' },
  { id: 'doc-2', title: 'Sales SDR Outreach Playbook & Qualification Criteria', type: 'DOCX', size: '1.1 MB', vectors: 850, date: '2026-07-18' },
  { id: 'doc-3', title: 'Product Architecture & API Reference Manual', type: 'MD', size: '480 KB', vectors: 620, date: '2026-07-20' },
  { id: 'doc-4', title: 'Customer Support Escalation SOP', type: 'PDF', size: '1.8 MB', vectors: 940, date: '2026-07-21' },
];

export default function KnowledgePage() {
  const [searchTerm, setSearchTerm] = useState('');

  const filteredDocs = sampleDocs.filter(d => d.title.toLowerCase().includes(searchTerm.toLowerCase()));

  return (
    <DashboardLayout>
      <div className="p-6 space-y-6 text-gray-800">
        <div className="flex items-center justify-between border-b pb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center">
              <Book className="w-6 h-6 mr-2 text-[#3c8dbc]" />
              RAG Knowledge Base & Document Store
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Vectorized document store grounding LangGraph agent prompts in enterprise truth.
            </p>
          </div>
          <button className="bg-[#3c8dbc] hover:bg-[#367fa9] text-white px-4 py-2 rounded text-sm font-semibold flex items-center shadow-sm">
            <Upload className="w-4 h-4 mr-2" />
            Upload Document
          </button>
        </div>

        {/* Vector DB Metrics */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <div className="bg-white p-4 rounded-lg border border-gray-200 shadow-sm flex items-center justify-between">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase">Vector Index</p>
              <h3 className="text-xl font-bold text-gray-900 mt-0.5">Qdrant Cluster</h3>
            </div>
            <div className="p-2 bg-purple-50 text-purple-600 rounded-lg"><Database className="w-5 h-5" /></div>
          </div>
          <div className="bg-white p-4 rounded-lg border border-gray-200 shadow-sm flex items-center justify-between">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase">Total Vectors</p>
              <h3 className="text-xl font-bold text-gray-900 mt-0.5">3,830 Embeddings</h3>
            </div>
            <div className="p-2 bg-blue-50 text-blue-600 rounded-lg"><FileText className="w-5 h-5" /></div>
          </div>
          <div className="bg-white p-4 rounded-lg border border-gray-200 shadow-sm flex items-center justify-between">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase">Embedding Model</p>
              <h3 className="text-xl font-bold text-emerald-600 mt-0.5">text-embedding-3-large</h3>
            </div>
            <div className="p-2 bg-emerald-50 text-emerald-600 rounded-lg"><CheckCircle2 className="w-5 h-5" /></div>
          </div>
        </div>

        {/* Search & Document List */}
        <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4 space-y-4">
          <div className="relative">
            <Search className="w-4 h-4 absolute left-3 top-3 text-gray-400" />
            <input
              type="text"
              placeholder="Search knowledge base documents or vector semantic chunks..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full pl-9 pr-4 py-2 border rounded-lg text-sm focus:outline-none focus:border-[#3c8dbc]"
            />
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm text-left">
              <thead className="bg-gray-50 text-xs font-semibold text-gray-500 border-b">
                <tr>
                  <th className="px-4 py-3">Document Title</th>
                  <th className="px-4 py-3">Format</th>
                  <th className="px-4 py-3">Size</th>
                  <th className="px-4 py-3">Vector Chunks</th>
                  <th className="px-4 py-3">Uploaded Date</th>
                </tr>
              </thead>
              <tbody className="divide-y text-gray-700">
                {filteredDocs.map(doc => (
                  <tr key={doc.id} className="hover:bg-gray-50 transition-colors">
                    <td className="px-4 py-3 font-medium text-gray-900 flex items-center">
                      <FileText className="w-4 h-4 mr-2 text-[#3c8dbc]" />
                      {doc.title}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-gray-500">{doc.type}</td>
                    <td className="px-4 py-3 text-gray-500">{doc.size}</td>
                    <td className="px-4 py-3 font-bold text-purple-600">{doc.vectors} chunks</td>
                    <td className="px-4 py-3 text-gray-400 text-xs">{doc.date}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </DashboardLayout>
  );
}
