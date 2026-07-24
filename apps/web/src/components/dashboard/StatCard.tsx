import React from 'react';
import { LucideIcon, ArrowUpRight, ArrowDownRight, Minus } from 'lucide-react';

interface StatCardProps {
  title: string;
  value: string | number;
  change?: string;
  trend?: 'up' | 'down' | 'neutral';
  icon: LucideIcon;
  iconColor?: string;
  iconBg?: string;
}

export default function StatCard({ 
  title, 
  value, 
  change = '+0%', 
  trend = 'up', 
  icon: Icon,
  iconColor = 'text-blue-600',
  iconBg = 'bg-blue-50 border-blue-100' 
}: StatCardProps) {
  return (
    <div className="bg-white rounded-xl border border-slate-200/80 p-5 hover:shadow-lg hover:border-slate-300 transition-all duration-200 flex flex-col justify-between">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">{title}</p>
          <h3 className="text-3xl font-extrabold text-slate-900 mt-2 tracking-tight">{value}</h3>
        </div>
        <div className={`p-3 rounded-xl border ${iconBg} shadow-xs`}>
          <Icon className={`h-5 w-5 ${iconColor}`} />
        </div>
      </div>

      <div className="flex items-center space-x-1.5 mt-4 pt-3 border-t border-slate-100">
        {trend === 'up' && <ArrowUpRight className="h-4 w-4 text-emerald-600" />}
        {trend === 'down' && <ArrowDownRight className="h-4 w-4 text-rose-600" />}
        {trend === 'neutral' && <Minus className="h-4 w-4 text-slate-400" />}
        
        <span className={`text-xs font-bold ${
          trend === 'up' ? 'text-emerald-600' : trend === 'down' ? 'text-rose-600' : 'text-slate-500'
        }`}>
          {change}
        </span>
        <span className="text-xs text-slate-400 font-medium">vs last 7 days</span>
      </div>
    </div>
  );
}
