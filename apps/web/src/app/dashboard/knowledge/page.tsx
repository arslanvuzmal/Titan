"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { BookOpen, Search, Upload, FileText, CheckCircle2 } from 'lucide-react';

export default function KnowledgePage() {
  return (
    <DashboardLayout>
      <div className="space-y-6 animate-fade-in">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-slate-200/80">
          <div>
            <h1 className="text-2xl font-bold text-slate-900 tracking-tight flex items-center">
              <BookOpen className="w-6 h-6 mr-2 text-blue-600" />
              Qdrant Vector Knowledge Base (RAG)
            </h1>
            <p className="text-xs text-slate-500 mt-1">Manage vector embeddings, PDF indexing, and hybrid semantic retrieval</p>
          </div>
          <button className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-xs font-semibold shadow-sm transition-colors flex items-center">
            <Upload className="w-3.5 h-3.5 mr-1.5" /> Upload Document
          </button>
        </div>

        {/* Search Bar */}
        <div className="bg-white p-4 rounded-xl border border-slate-200/80 shadow-xs flex items-center space-x-3">
          <Search className="w-5 h-5 text-slate-400" />
          <input
            type="text"
            placeholder="Search indexed vector documents using semantic cosine similarity..."
            className="w-full text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none"
          />
        </div>

        {/* Document Table */}
        <div className="bg-white rounded-xl border border-slate-200/80 shadow-xs overflow-hidden">
          <div className="px-6 py-4 border-b border-slate-100 bg-slate-50/50">
            <h3 className="font-bold text-slate-900 text-sm">Indexed Document Repository</h3>
          </div>
          <table className="w-full text-xs text-left">
            <thead className="bg-slate-50 text-slate-500 font-semibold border-b border-slate-100 uppercase tracking-wider">
              <tr>
                <th className="px-6 py-3">Document Name</th>
                <th className="px-6 py-3">Collection</th>
                <th className="px-6 py-3">Vector Chunks</th>
                <th className="px-6 py-3">Embedding Model</th>
                <th className="px-6 py-3">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 bg-white">
              {[
                { name: 'TITAN_Architecture_Spec_2026.pdf', collection: 'technical_docs', chunks: '1,420', model: 'text-embedding-3-large', status: 'Indexed' },
                { name: 'Q3_Financial_Audit_Report.pdf', collection: 'finance', chunks: '850', model: 'text-embedding-3-large', status: 'Indexed' },
                { name: 'Enterprise_SLA_Terms_v4.pdf', collection: 'legal', chunks: '320', model: 'text-embedding-3-large', status: 'Indexed' },
              ].map((doc) => (
                <tr key={doc.name} className="hover:bg-slate-50 transition-colors">
                  <td className="px-6 py-3.5 text-slate-900 font-semibold flex items-center">
                    <FileText className="w-4 h-4 mr-2 text-blue-500" /> {doc.name}
                  </td>
                  <td className="px-6 py-3.5 text-slate-600 font-mono">{doc.collection}</td>
                  <td className="px-6 py-3.5 text-slate-600 font-mono">{doc.chunks}</td>
                  <td className="px-6 py-3.5 text-slate-500">{doc.model}</td>
                  <td className="px-6 py-3.5">
                    <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">
                      <CheckCircle2 className="w-3 h-3 mr-1" /> {doc.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </DashboardLayout>
  );
}
