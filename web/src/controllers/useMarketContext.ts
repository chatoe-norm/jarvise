import { useCallback, useEffect, useRef, useState } from "react";
import type { SemiDynamicContext } from "@/types/marketContext";
import { marketContextService } from "@/services/marketContextService";
import { marketContextRepository } from "@/repositories/marketContextRepository";
import type { ApprovalRow, NewsHome } from "@/lib/api";

export interface UseMarketContextProps {
  score?: number | null;
  news?: NewsHome | null;
  approvals?: ApprovalRow[];
}

export function useMarketContext({ score, news, approvals }: UseMarketContextProps) {
  // Stale-While-Revalidate: Initialize from local storage cache for <0.1s instant render
  const [context, setContext] = useState<SemiDynamicContext | null>(() => {
    return marketContextRepository.getCachedContext();
  });

  const [isOpen, setIsOpen] = useState(false);
  const [isMobile, setIsMobile] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return (
      window.innerWidth < 768 ||
      (window.matchMedia && !window.matchMedia("(hover: hover)").matches)
    );
  });

  // Track responsive breakpoint changes
  useEffect(() => {
    if (typeof window === "undefined") return;
    const updateMobile = () => {
      const mobile =
        window.innerWidth < 768 ||
        (window.matchMedia && !window.matchMedia("(hover: hover)").matches);
      setIsMobile(mobile);
    };

    window.addEventListener("resize", updateMobile);
    return () => window.removeEventListener("resize", updateMobile);
  }, []);

  // Update & compose context whenever inputs change
  useEffect(() => {
    if (typeof score !== "number" || !Number.isFinite(score)) {
      return;
    }

    try {
      const fresh = marketContextService.composeSemiDynamicContext({
        score,
        news,
        approvals,
      });
      setContext(fresh);
      marketContextRepository.saveCachedContext(fresh);
    } catch (err) {
      console.error("[useMarketContext] Failed to compose context:", err);
    }
  }, [score, news, approvals]);

  // Count-up animation for score
  const targetScore = typeof score === "number" && Number.isFinite(score) ? score : (context?.score ?? 0);
  const [displayScore, setDisplayScore] = useState<number>(() => (context ? context.score : 0));
  const animRef = useRef<number | null>(null);

  useEffect(() => {
    if (typeof targetScore !== "number") return;
    const start = displayScore;
    const diff = targetScore - start;
    if (diff === 0) return;

    const duration = 650; // ms
    const startTime = performance.now();

    const animate = (currentTime: number) => {
      const elapsed = currentTime - startTime;
      const progress = Math.min(elapsed / duration, 1);
      // Ease out cubic
      const easeProgress = 1 - Math.pow(1 - progress, 3);
      const currentVal = Math.round(start + diff * easeProgress);
      setDisplayScore(currentVal);

      if (progress < 1) {
        animRef.current = requestAnimationFrame(animate);
      }
    };

    animRef.current = requestAnimationFrame(animate);

    return () => {
      if (animRef.current) cancelAnimationFrame(animRef.current);
    };
  }, [targetScore]);

  // Haptic feedback trigger for mobile
  const triggerHaptic = useCallback(() => {
    if (typeof window !== "undefined" && typeof navigator !== "undefined" && "vibrate" in navigator) {
      try {
        navigator.vibrate(15);
      } catch {
        // Safe ignore
      }
    }
  }, []);

  const openDetails = useCallback(() => {
    triggerHaptic();
    setIsOpen(true);
  }, [triggerHaptic]);

  const closeDetails = useCallback(() => {
    setIsOpen(false);
  }, []);

  const toggleDetails = useCallback(() => {
    setIsOpen((prev) => {
      if (!prev) triggerHaptic();
      return !prev;
    });
  }, [triggerHaptic]);

  // Quick Action: Dismiss sheet & smooth scroll to Approval Queue
  const scrollToApprovalQueue = useCallback(() => {
    setIsOpen(false);
    if (typeof document === "undefined") return;

    const target = document.getElementById("approval-queue");
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      target.focus?.();
    }
  }, []);

  return {
    context,
    displayScore,
    targetScore,
    isOpen,
    isMobile,
    openDetails,
    closeDetails,
    toggleDetails,
    scrollToApprovalQueue,
  };
}
