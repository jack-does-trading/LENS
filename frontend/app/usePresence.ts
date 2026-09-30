"use client";

import { useEffect, useState } from "react";
import { recordPresence, type Presence } from "./api";

/** Heartbeat cadence. The backend's online window is 75s, so one dropped
 *  beat never flickers a visitor out of the count and back in. */
const HEARTBEAT_MS = 30_000;
const SESSION_KEY = "lens_session_id";

/** sessionStorage, not localStorage: "visits" should count a new tab as a new
 *  visit, which is what the number conventionally means. Wrapped because
 *  storage access itself throws in some privacy modes. */
function sessionId(): string | null {
  try {
    const existing = sessionStorage.getItem(SESSION_KEY);
    if (existing) return existing;
    const minted = crypto.randomUUID();
    sessionStorage.setItem(SESSION_KEY, minted);
    return minted;
  } catch {
    return null;
  }
}

/**
 * Live site counters for the shelf's status cluster.
 *
 * Returns null until the first heartbeat lands, and stays null if the backend
 * is asleep or unreachable -- the caller renders nothing rather than a zero,
 * so a cold Render instance never flashes "0 visits" at a real visitor.
 */
export function usePresence(): Presence | null {
  const [presence, setPresence] = useState<Presence | null>(null);

  useEffect(() => {
    const id = sessionId();
    if (!id) return;

    let cancelled = false;

    const beat = async () => {
      // A hidden tab is not a person looking at the site. Skipping the beat
      // (rather than sending one) lets a backgrounded tab age out of the
      // online window on its own, which keeps "online" honest.
      if (document.hidden) return;
      try {
        const next = await recordPresence(id);
        if (!cancelled) setPresence(next);
      } catch {
        // Backend asleep or offline -- keep the last known numbers, and the
        // next beat will reconcile. Never surfaced to the user.
      }
    };

    void beat();
    const timer = setInterval(beat, HEARTBEAT_MS);
    // Coming back to the tab should update immediately, not up to 30s later.
    document.addEventListener("visibilitychange", beat);

    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", beat);
    };
  }, []);

  return presence;
}
