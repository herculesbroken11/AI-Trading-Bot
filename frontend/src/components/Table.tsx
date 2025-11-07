import React from 'react';
import { cn } from '../lib/utils';

interface TableProps {
  children: React.ReactNode;
  className?: string;
}

export const Table: React.FC<TableProps> = ({ children, className }) => {
  return (
    <div className="overflow-x-auto">
      <table className={cn('w-full border-collapse', className)}>
        {children}
      </table>
    </div>
  );
};

export const TableHeader: React.FC<TableProps> = ({ children, className }) => {
  return (
    <thead className={cn('bg-muted', className)}>
      {children}
    </thead>
  );
};

export const TableBody: React.FC<TableProps> = ({ children }) => {
  return <tbody>{children}</tbody>;
};

export const TableRow: React.FC<TableProps> = ({ children, className }) => {
  return (
    <tr className={cn('border-b hover:bg-muted/50', className)}>
      {children}
    </tr>
  );
};

export const TableHead: React.FC<TableProps> = ({ children, className }) => {
  return (
    <th className={cn('px-4 py-3 text-left font-semibold text-sm', className)}>
      {children}
    </th>
  );
};

export const TableCell: React.FC<TableProps> = ({ children, className }) => {
  return (
    <td className={cn('px-4 py-3 text-sm', className)}>
      {children}
    </td>
  );
};

