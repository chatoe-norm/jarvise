import { useCallback } from "react";

export function useHapticFeedback(defaultDurationMs: number = 15) {
  const triggerHaptic = useCallback(
    (durationMs: number = defaultDurationMs) => {
      if (typeof window !== "undefined" && typeof navigator !== "undefined" && "vibrate" in navigator) {
        try {
          navigator.vibrate(durationMs);
        } catch {
          // Ignore devices/browsers that do not support or allow vibration
        }
      }
    },
    [defaultDurationMs],
  );

  return { triggerHaptic };
}
