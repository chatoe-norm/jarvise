import { useCallback, useEffect, useRef, useState } from "react";
import type { SemiDynamicContext } from "@/types/marketContext";
import { marketContextService } from "@/services/marketContextService";
import { marketContextRepository } from "@/repositories/marketContextRepository";
import { useHapticFeedback } from "@/controllers/useHapticFeedback";
import type { NewsHome } from "@/lib/api";

export interface UseMarketContextProps {
  /** undefined = dashboard not loaded yet; null = loaded, but no Fear & Greed value. */
  score?: number | null;
  news?: NewsHome | null;
}

export function useMarketContext({ score, news }: UseMarketContextProps) {
  // Stale-While-Revalidate: a recent (TTL-bounded) cache only bridges first paint
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
    if (score === null) {
      // The server answered with no value: drop any cached label so it can't sit beside "—".
      setContext(null);
      return;
    }
    if (typeof score !== "number" || !Number.isFinite(score)) {
      return;
    }

    try {
      const fresh = marketContextService.composeSemiDynamicContext({ score, news });
      setContext(fresh);
      marketContextRepository.saveCachedContext(fresh);
    } catch (err) {
      console.error("[useMarketContext] Failed to compose context:", err);
    }
  }, [score, news]);

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
  const { triggerHaptic } = useHapticFeedback(15);

  const [modalTab, setModalTab] = useState<"fear_greed" | "altcoin">("fear_greed");

  const openFearGreed = useCallback(() => {
    setModalTab("fear_greed");
    triggerHaptic();
    setIsOpen(true);
  }, [triggerHaptic]);

  const openAltcoinSeason = useCallback(() => {
    setModalTab("altcoin");
    triggerHaptic();
    setIsOpen(true);
  }, [triggerHaptic]);

  const closeDetails = useCallback(() => {
    setIsOpen(false);
  }, []);

  // Close modal on Escape key press
  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setIsOpen(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen]);

  return {
    context,
    displayScore,
    isOpen,
    isMobile,
    modalTab,
    setModalTab,
    openFearGreed,
    openAltcoinSeason,
    closeDetails,
  };
}
