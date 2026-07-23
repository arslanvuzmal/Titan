import React from 'react';
import TaskDetailsClient from '@/components/operations/TaskDetailsClient';

export function generateStaticParams() {
  return [
    { taskId: 'demo-task-1' },
    { taskId: 'demo-task-2' },
  ];
}

export default async function TaskDetailsPage({ params }: { params: Promise<{ taskId: string }> }) {
  const { taskId } = await params;
  return <TaskDetailsClient taskId={taskId} />;
}
