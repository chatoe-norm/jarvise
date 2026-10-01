import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatNum(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  return n.toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  });
}

export function relativeAge(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  const age = Date.now() - ms;
  if (age < 0) return "soon";
  const sec = Math.floor(age / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `${hr}h ago`;
  return `${Math.floor(hr / 24)}d ago`;
}

export function expiresIn(expiresAtMs: number | null | undefined): string {
  if (expiresAtMs == null) return "—";
  const left = expiresAtMs - Date.now();
  if (left <= 0) return "expired";
  const min = Math.floor(left / 60_000);
  if (min < 60) return `${min}m left`;
  return `${Math.floor(min / 60)}h ${min % 60}m left`;
}
