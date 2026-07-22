import React from 'react';

interface StatCardProps {
  title: string;
  value: string | number;
  icon: React.ReactNode;
  colorClass: string; // e.g., 'bg-[#00c0ef]', 'bg-[#00a65a]'
  trend?: string;
}

export default function StatCard({ title, value, icon, colorClass }: StatCardProps) {
  return (
    <div className={`rounded shadow-sm text-white overflow-hidden ${colorClass}`}>
      <div className="p-4 flex items-center justify-between relative">
        <div className="z-10">
          <h3 className="text-3xl font-bold">{value}</h3>
          <p className="text-sm mt-1 opacity-90 uppercase tracking-wide">{title}</p>
        </div>
        <div className="z-10 opacity-30 scale-150 mr-4">
          {icon}
        </div>
      </div>
      <div className="bg-black bg-opacity-10 py-1 text-center text-sm flex justify-center items-center hover:bg-opacity-20 cursor-pointer transition-colors">
        More info <span className="ml-1">➔</span>
      </div>
    </div>
  );
}
