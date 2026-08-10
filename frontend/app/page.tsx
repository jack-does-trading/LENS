"use client";

import dynamic from "next/dynamic";
import { useEffect, useRef, useState } from "react";
import {
  Analysis,
  Book,
  Principle,
  Streak,
  Suggestion,
  UserRec,
  createAnalysis,
  createDailyLog,
  getOrCreateUser,
  getPrinciple,
  getStreak,
  listBooks,
  listSuggestions,
  setActiveBook,
  updateSuggestion,
} from "./api";
import { BOOK_CATALOG_FALLBACK } from "./bookCatalogFallback";

// WebGL only exists in the browser, so the shelf never renders on the server.
const BookShelf3D = dynamic(() => import("@/components/BookShelf3D/BookShelf3D"), {
  ssr: false,
});

// Shown one at a time while the model runs, so the wait reads as a pause for
// thought rather than dead time.
const LOADING_QUOTES: { text: string; by?: string }[] = [
  { text: "We suffer more often in imagination than in reality.", by: "Seneca" },
  { text: "Man is disturbed not by things, but by the views he takes of them.", by: "Epictetus" },
  { text: "You have power over your mind — not outside events. Realise this, and you will find strength.", by: "Marcus Aurelius" },
  { text: "The impediment to action advances action. What stands in the way becomes the way.", by: "Marcus Aurelius" },
  { text: "First say to yourself what you would be; and then do what you have to do.", by: "Epictetus" },
  { text: "How we spend our days is, of course, how we spend our lives.", by: "Annie Dillard" },
  { text: "The unexamined life is not worth living.", by: "Socrates" },
  { text: "Reading your situation against the principles in this book." },
  { text: "Weighing what the book actually says before it says anything to you." },
];

const MOOD_LABELS = ["Rough", "Low", "Even", "Good", "Great"];

// A reflection belongs to the book it was generated for -- switching books
// mid-session must hide it (never show book A's advice while book B is
// selected), but switching back to book A should bring it right back rather
// than losing it. Keyed by book_id so each book keeps its own last result.
type BookResult = {
  analysis: Analysis;
  relatedPrinciples: Principle[];
  suggestions: Suggestion[];
};

function supportsWebGL() {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(
      window.WebGLRenderingContext &&
        (canvas.getContext("webgl2") || canvas.getContext("webgl")),
    );
  } catch {
    return false;
  }
}

export default function Home() {
  const [user, setUser] = useState<UserRec | null>(null);
  // Starts as a real snapshot of the reviewed catalog (not placeholder
  // data) so the shelf is explorable immediately, before the backend has
  // answered at all -- see bookCatalogFallback.ts and Architecture.md's
  // "Optimistic shelf render" addendum.
  const [books, setBooks] = useState<Book[]>(BOOK_CATALOG_FALLBACK);
  const [bookId, setBookId] = useState("");
  const [category, setCategory] = useState("");
  const [action, setAction] = useState("");
  const [mood, setMood] = useState(3);

  const [resultsByBook, setResultsByBook] = useState<Record<string, BookResult>>({});
  // Set only right after a fresh analysis is generated -- distinct from
  // `analysis` below, which also changes (to a possibly-older result) just
  // from switching books. Only a genuinely new reflection should auto-scroll.
  const [justSubmittedId, setJustSubmittedId] = useState<string | null>(null);
  const [streak, setStreak] = useState<Streak | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // null until the browser has been probed, so the flat fallback never flashes.
  const [webglReady, setWebglReady] = useState<boolean | null>(null);
  const [heroDismissed, setHeroDismissed] = useState(false);
  const [quoteIndex, setQuoteIndex] = useState(0);
  // True once the live user + book list have actually loaded, replacing
  // the static fallback snapshot above. Exploring the shelf never waits on
  // this -- only an action that needs a real backend round-trip does (see
  // handleSubmit), and only if it's attempted before this flips.
  const [backendReady, setBackendReady] = useState(false);
  // Shown only while something is actively waiting on the backend and it's
  // taking long enough to suggest a cold Render free-tier instance waking
  // up (~30-60s) rather than a normal request. Set by handleSubmit, not by
  // the silent background bootstrap -- there's nothing on-screen to attach
  // a "waking up" message to while the user is just browsing the shelf.
  const [waking, setWaking] = useState(false);

  const resultRef = useRef<HTMLDivElement>(null);
  // Dedupes the user+books fetch: the background kick-off on mount and a
  // handleSubmit that fires before it's resolved both await the SAME
  // in-flight request rather than firing two.
  const bootstrapRef = useRef<Promise<{ user: UserRec; books: Book[] }> | null>(null);

  const currentResult = bookId ? resultsByBook[bookId] : undefined;
  const analysis = currentResult?.analysis ?? null;
  const relatedPrinciples = currentResult?.relatedPrinciples ?? [];
  const suggestions = currentResult?.suggestions ?? [];

  function bootstrap(): Promise<{ user: UserRec; books: Book[] }> {
    if (!bootstrapRef.current) {
      bootstrapRef.current = (async () => {
        const u = await getOrCreateUser();
        const allBooks = await listBooks();
        const reviewed = allBooks.filter((b) => b.review_status === "human_reviewed");
        setUser(u);
        setBooks(reviewed);
        setBackendReady(true);
        // Preserve whatever the user already picked while exploring the
        // fallback shelf -- only default it if nothing's been chosen yet.
        setBookId((prev) => prev || u.active_book_id || reviewed[0]?.book_id || "");
        return { user: u, books: reviewed };
      })();
    }
    return bootstrapRef.current;
  }

  useEffect(() => setWebglReady(supportsWebGL()), []);

  // Fires the instant the page loads, in the background -- gives Render's
  // free-tier instance the maximum possible head start on waking up before
  // the user finishes exploring and actually asks for advice.
  useEffect(() => {
    bootstrap().catch((e) => setError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (user && bookId) {
      getStreak(user.user_id, bookId).then(setStreak).catch(() => setStreak(null));
    }
  }, [user, bookId]);

  // Any scroll-ish gesture retires the opening title and hands the shelf over
  // to the user. The canvas swallows wheel events itself, so this listens on
  // the capture phase to see the very first one.
  useEffect(() => {
    if (heroDismissed || !webglReady) return;
    const dismiss = () => setHeroDismissed(true);
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY > 0 || e.deltaX !== 0) dismiss();
    };
    const onKey = (e: KeyboardEvent) => {
      if (["ArrowDown", "ArrowRight", "PageDown", "Enter", " ", "Tab"].includes(e.key)) dismiss();
    };
    let touchStartY = 0;
    const onTouchStart = (e: TouchEvent) => {
      touchStartY = e.touches[0]?.clientY ?? 0;
    };
    const onTouchMove = (e: TouchEvent) => {
      if (touchStartY - (e.touches[0]?.clientY ?? 0) > 24) dismiss();
    };
    const opts = { capture: true, passive: true } as const;
    window.addEventListener("wheel", onWheel, opts);
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("touchstart", onTouchStart, opts);
    window.addEventListener("touchmove", onTouchMove, opts);
    return () => {
      window.removeEventListener("wheel", onWheel, opts);
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("touchstart", onTouchStart, opts);
      window.removeEventListener("touchmove", onTouchMove, opts);
    };
  }, [heroDismissed, webglReady]);

  useEffect(() => {
    if (!loading) return;
    setQuoteIndex(Math.floor(Math.random() * LOADING_QUOTES.length));
    const id = setInterval(
      () => setQuoteIndex((i) => (i + 1) % LOADING_QUOTES.length),
      3600,
    );
    return () => clearInterval(id);
  }, [loading]);

  // A finished reflection slides itself to the top of the panel and fills it;
  // the inputs stay one scroll-up away rather than being replaced. Fires only
  // for a just-generated reflection, not merely from switching to a book
  // that already has an older one on file.
  useEffect(() => {
    if (!justSubmittedId) return;
    const id = setTimeout(
      () => resultRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }),
      120,
    );
    return () => clearTimeout(id);
  }, [justSubmittedId]);

  async function handleBookChange(newBookId: string) {
    if (newBookId === bookId) return;
    setBookId(newBookId);
    if (user) await setActiveBook(user.user_id, newBookId).catch(() => {});
  }

  async function handleSubmit() {
    if (!bookId || !action.trim()) return;
    // Captured so a book switch mid-request can't misfile the result under
    // whatever book happens to be selected by the time it comes back.
    const activeBookId = bookId;
    setLoading(true);
    setError("");
    setJustSubmittedId(null);
    // Drop this book's old result so the loading state doesn't sit above a
    // stale reflection while the new one is generated.
    setResultsByBook((prev) => {
      if (!(activeBookId in prev)) return prev;
      const next = { ...prev };
      delete next[activeBookId];
      return next;
    });

    let wakeTimer: ReturnType<typeof setTimeout> | null = null;
    try {
      // The user explored the shelf off the static fallback catalog faster
      // than the backend answered -- this is the one action that actually
      // needs it, so wait here (and only here) for the same in-flight
      // bootstrap the mount effect already kicked off.
      let activeUserId = user?.user_id;
      if (!activeUserId) {
        wakeTimer = setTimeout(() => setWaking(true), 4000);
        const bootstrapResult = await bootstrap();
        activeUserId = bootstrapResult.user.user_id;
      }

      const now = new Date();
      // Each ask is its own independent situation -- a fresh daily_logs row
      // and its own analysis every time, never merged with earlier asks the
      // same day (daily_logs no longer has a per-day uniqueness constraint,
      // see migration 006).
      const log = await createDailyLog({
        user_id: activeUserId,
        date: now.toISOString().slice(0, 10),
        chosen_book_id: activeBookId,
        entries: [{ time: now.toTimeString().slice(0, 5), action, category: category || "general" }],
        mood,
      });

      const result = await createAnalysis(log.log_id);
      const fetchedSuggestions = await listSuggestions(result.analysis_id);
      setResultsByBook((prev) => ({
        ...prev,
        [activeBookId]: { analysis: result, suggestions: fetchedSuggestions, relatedPrinciples: [] },
      }));
      setJustSubmittedId(result.analysis_id);

      Promise.all(result.retrieved_principle_ids.map(getPrinciple))
        .then((principles) => {
          setResultsByBook((prev) => {
            const existing = prev[activeBookId];
            // Guards against a slower-to-resolve fetch overwriting a newer
            // result for the same book (or one for a book since removed).
            if (!existing || existing.analysis.analysis_id !== result.analysis_id) return prev;
            return { ...prev, [activeBookId]: { ...existing, relatedPrinciples: principles } };
          });
        })
        .catch(() => {});
      getStreak(activeUserId, activeBookId).then(setStreak).catch(() => {});

      // Reset the form so the next ask reads as a new, unrelated situation.
      setAction("");
      setCategory("");
      setMood(3);
    } catch (e) {
      setError(String(e));
    } finally {
      if (wakeTimer) clearTimeout(wakeTimer);
      setWaking(false);
      setLoading(false);
    }
  }

  async function markSuggestion(id: string, status: "done" | "skipped") {
    const updated = await updateSuggestion(id, status);
    setResultsByBook((prev) => {
      const existing = bookId ? prev[bookId] : undefined;
      if (!existing) return prev;
      return {
        ...prev,
        [bookId]: {
          ...existing,
          suggestions: existing.suggestions.map((s) => (s.suggestion_id === id ? updated : s)),
        },
      };
    });
  }

  const quote = LOADING_QUOTES[quoteIndex];

  const form = (
    <div className="ask-form">
      {streak && (
        <p className="ask-form__streak">
          Streak {streak.current_streak_days}d · longest {streak.longest_streak_days}d
        </p>
      )}

      <label className="field">
        <span className="field__label">Category</span>
        <input
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          placeholder="work, relationships, health…"
        />
      </label>

      <label className="field">
        <span className="field__label">What&apos;s happening?</span>
        <textarea
          rows={4}
          value={action}
          onChange={(e) => setAction(e.target.value)}
          placeholder="Describe the situation in your own words."
        />
      </label>

      <div className="field">
        <span className="field__label">
          Mood
          <em className="field__value">{MOOD_LABELS[mood - 1]}</em>
        </span>
        <input
          className="mood-slider"
          type="range"
          min={1}
          max={5}
          step={1}
          value={mood}
          onChange={(e) => setMood(Number(e.target.value))}
          aria-label={`Mood, ${mood} of 5`}
        />
        <div className="mood-scale" aria-hidden="true">
          {[1, 2, 3, 4, 5].map((n) => (
            <span key={n} className={n === mood ? "is-active" : ""}>
              {n}
            </span>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="advice-loading" role="status" aria-live="polite">
          <div className="advice-loading__bar">
            <span />
          </div>
          {waking ? (
            <p className="advice-loading__waking">
              Waking up the server — this can take up to a minute on the free tier…
            </p>
          ) : (
            <blockquote key={quoteIndex} className="advice-loading__quote">
              {quote.text}
              {quote.by && <cite>{quote.by}</cite>}
            </blockquote>
          )}
        </div>
      ) : (
        <button type="button" className="advice-button" onClick={handleSubmit} disabled={!action.trim()}>
          <span>Get advice</span>
          <span aria-hidden="true">↗</span>
        </button>
      )}

      {error && <p className="error">{error}</p>}
    </div>
  );

  const result = analysis && (
    <div className="ask-result" ref={resultRef}>
      <p className="eyebrow">REFLECTION</p>

      {relatedPrinciples.length > 0 && (
        <>
          <p className="muted">Your situation relates to these principles:</p>
          <div className="principle-chips">
            {relatedPrinciples.map((p) => (
              <span key={p.principle_id} className="principle-chip">
                {p.name}
              </span>
            ))}
          </div>
        </>
      )}

      <div className="reflection-summary">{analysis.reflection}</div>

      {analysis.verification_status === "fallback_used" && (
        <p className="muted">
          (The AI model couldn&apos;t produce a personalized reflection that passed grounding checks today, so
          this is assembled directly from the book&apos;s own reviewed text instead of generated freely.)
        </p>
      )}

      <p className="eyebrow eyebrow--tips">TOP 3 TIPS</p>
      {suggestions.slice(0, 3).map((s) => (
        <div key={s.suggestion_id} className={`suggestion ${s.status === "done" ? "done" : ""}`}>
          <div className="suggestion-body">
            <strong>{s.text}</strong>
            <p className="muted">{s.explanation}</p>
          </div>
          <div className="suggestion-actions">
            <button onClick={() => markSuggestion(s.suggestion_id, "done")} disabled={s.status !== "pending"}>
              Done
            </button>
            <button onClick={() => markSuggestion(s.suggestion_id, "skipped")} disabled={s.status !== "pending"}>
              Skip
            </button>
          </div>
        </div>
      ))}
    </div>
  );

  if (webglReady === null) return <main className="stage" />;

  // Flat fallback: no WebGL, so no shelf -- an ordinary scrolling page instead.
  if (!webglReady) {
    return (
      <main className="classic-page">
        <h1>Lens</h1>
        <p className="muted">Read your day through the book you&apos;re reading.</p>

        {backendReady && books.length === 0 && !error && (
          <p className="muted">No reviewed books available yet.</p>
        )}

        {books.length > 0 && (
          <>
            <label className="field">
              <span className="field__label">Book</span>
              <select value={bookId} onChange={(e) => handleBookChange(e.target.value)}>
                {books.map((b) => (
                  <option key={b.book_id} value={b.book_id}>
                    {b.title}
                  </option>
                ))}
              </select>
            </label>
            {form}
          </>
        )}

        {error && books.length === 0 && <p className="error">{error}</p>}
        {result}
        <p className="site-footer site-footer--classic">made with love ❤️ by @bhavyadeep</p>
      </main>
    );
  }

  return (
    <main className="stage">
      {books.length > 0 ? (
        <BookShelf3D
          books={books}
          bookId={bookId}
          onSelect={handleBookChange}
          hero={!heroDismissed}
          onExitHero={() => setHeroDismissed(true)}
          onGoHome={() => setHeroDismissed(false)}
        >
          {form}
          {result}
        </BookShelf3D>
      ) : (
        <div className="stage-empty">
          <h1 className="stage-empty__mark">LENS</h1>
          <p className="muted">{error || "No reviewed books available yet."}</p>
        </div>
      )}

      {/* Kept mounted as the keyboard/screen-reader path into the same shelf. */}
      <label className="visually-hidden">
        Book
        <select value={bookId} onChange={(e) => handleBookChange(e.target.value)}>
          {books.map((b) => (
            <option key={b.book_id} value={b.book_id}>
              {b.title}
            </option>
          ))}
        </select>
      </label>
    </main>
  );
}
