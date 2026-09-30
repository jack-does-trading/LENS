"use client";

import { useEffect, useState } from "react";
import { getQualityMetrics, type QualityMetrics } from "./api";

/**
 * Live pipeline health, for the shelf's status cluster and the benchmarks page.
 *
 * Same contract as usePresence: null until a response lands, and null forever if
 * the backend is asleep or unreachable. The caller renders nothing rather than a
 * zero -- "0% grounded" on a cold Render instance would be a lie about the
 * pipeline rather than a fact about the fetch.
 *
 * Fetched once, not polled. Unlike presence, these counters move only when
 * somebody submits a situation, so a heartbeat would spend requests to watch a
 * number that is usually still.
 */
export function useQuality(windowDays = 30): QualityMetrics | null {
  const [quality, setQuality] = useState<QualityMetrics | null>(null);

  useEffect(() => {
    let cancelled = false;
    getQualityMetrics(windowDays)
      .then((next) => {
        if (!cancelled) setQuality(next);
      })
      .catch(() => {
        // Backend asleep, or predates the metrics route. Not surfaced.
      });
    return () => {
      cancelled = true;
    };
  }, [windowDays]);

  return quality;
}
