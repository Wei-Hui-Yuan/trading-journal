'use client';

import React, { useEffect, useRef, useState } from 'react';
import { Plus } from 'lucide-react';

/**
 * A text input that also offers values already used elsewhere in the book --
 * write anything, or pick one from the list. There is no separate "add"
 * step: whatever is typed IS the value, exactly like the plain input this
 * replaces. The dropdown exists so a second Technology does not get typed as
 * "Tech" by accident, not to restrict what can be entered.
 */
export const Combobox: React.FC<
  {
    value: string;
    onChange: (value: string) => void;
    options: string[];
  } & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'>
> = ({ value, onChange, options, className, ...inputProps }) => {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onClickOutside);
    return () => document.removeEventListener('mousedown', onClickOutside);
  }, [open]);

  const query = value.trim().toLowerCase();
  const filtered = query
    ? options.filter((o) => o.toLowerCase().includes(query))
    : options;
  const exactMatch = options.some((o) => o.toLowerCase() === query);
  const showAddNew = query.length > 0 && !exactMatch;

  return (
    <div ref={wrapperRef} className="relative">
      <input
        {...inputProps}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') setOpen(false);
        }}
        autoComplete="off"
        className={className}
      />
      {open && (filtered.length > 0 || showAddNew) && (
        <div className="absolute z-20 mt-1 max-h-48 w-full overflow-y-auto rounded-lg border border-obsidian-border bg-obsidian-card py-1 shadow-xl">
          {showAddNew && (
            <button
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => setOpen(false)}
              className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-xs text-emerald-300 hover:bg-emerald-500/10"
            >
              <Plus className="h-3 w-3 shrink-0" />
              Add &quot;{value.trim()}&quot;
            </button>
          )}
          {filtered.map((option) => (
            <button
              key={option}
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                onChange(option);
                setOpen(false);
              }}
              className="block w-full truncate px-3 py-1.5 text-left text-xs text-slate-200 hover:bg-slate-700/25"
            >
              {option}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

export default Combobox;
