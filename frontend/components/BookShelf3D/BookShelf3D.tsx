"use client";

// Adapted for Lens from "The Complete Shelf" by Mint (MIT License).
// Source: https://github.com/mintdotgg/mint-playground/tree/main/experiences/complete-shelf
// See LICENSE.mint in this directory.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Book } from "@/app/api";
import { toCatalog } from "./lensCatalog";
import { ShelfEngine, type ShelfMode } from "./ShelfEngine";

function ArrowIcon({ direction }: { direction: "left" | "right" }) {
  return (
    <span aria-hidden="true" className={`arrow-icon arrow-icon--${direction}`}>
      <span />
    </span>
  );
}

type Props = {
  books: Book[];
  bookId: string;
  onSelect: (bookId: string) => void;
  /** Opening state: oversized title, shelf idling untouched behind it. */
  hero: boolean;
  onExitHero: () => void;
  /** Wordmark click/Enter: close any open book and bring back the hero. */
  onGoHome: () => void;
  /** Rendered inside the opened book's panel, under its description. */
  children?: ReactNode;
};

export default function BookShelf3D({
  books,
  bookId,
  onSelect,
  hero,
  onExitHero,
  onGoHome,
  children,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<ShelfEngine | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [mode, setMode] = useState<ShelfMode>("browse");
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState("Preparing the shelf");

  const catalog = useMemo(() => toCatalog(books), [books]);
  const activeBook = catalog[activeIndex];
  const selectedBook = selectedIndex === null ? null : catalog[selectedIndex];
  const isFocused = mode !== "browse";

  // The engine is built once per catalog, so its callbacks read the latest
  // onSelect through a ref rather than closing over a stale render's copy.
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  useEffect(() => {
    let cancelled = false;
    let engine: ShelfEngine | null = null;

    async function start() {
      if (!canvasRef.current || catalog.length === 0) return;
      await document.fonts.ready;
      if (cancelled || !canvasRef.current) return;

      engine = new ShelfEngine(canvasRef.current, catalog, {
        onActiveIndex: setActiveIndex,
        onMode: (nextMode, index) => {
          setMode(nextMode);
          setSelectedIndex(index);
          // Opening a book is the selection -- the panel that slides in is
          // already the form for that book, so there is nothing to confirm.
          if (index !== null && (nextMode === "focusing" || nextMode === "inspect")) {
            const opened = catalog[index];
            if (opened) onSelectRef.current(opened.id);
          }
        },
        onStatus: setStatus,
        onReady: () => setReady(true),
      });
      engineRef.current = engine;
    }

    void start();
    return () => {
      cancelled = true;
      engine?.dispose();
      engineRef.current = null;
    };
  }, [catalog]);

  // Keep the shelf pointed at whatever the rest of the page considers active,
  // including the initial book restored from the user's saved preference.
  useEffect(() => {
    const index = catalog.findIndex((entry) => entry.id === bookId);
    if (index >= 0 && index !== activeIndex) {
      engineRef.current?.browseTo(index);
    }
    // Only react to an externally driven book change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bookId, catalog]);

  // Closes any open/inspecting book and hands the hero copy back -- the
  // wordmark's "return home" gesture.
  function goHome() {
    if (mode !== "browse") engineRef.current?.returnToShelf();
    onGoHome();
  }

  if (catalog.length === 0) return null;

  return (
    <div
      className={`shelf-experience ${ready ? "is-ready" : ""} ${
        isFocused ? "is-focused" : "is-browsing"
      } ${hero ? "is-hero" : "is-open"}`}
    >
      <canvas
        ref={canvasRef}
        className="shelf-canvas"
        data-testid="shelf-canvas"
        role="application"
        tabIndex={0}
        aria-label={`Interactive three-dimensional shelf of ${catalog.length} books. Drag or use the arrow keys to browse. Press Enter to inspect the selected book.`}
      />

      <div className="hero-scrim" aria-hidden="true" />

      {/* The wordmark is one element in two positions: centred and oversized to
          open with, then parked small in the top-left once the user scrolls.
          It's also the "return home" control once parked -- closes any open
          book and brings the hero copy back. */}
      <h1
        className="lens-mark"
        role="button"
        tabIndex={hero ? -1 : 0}
        aria-label="Return to the shelf home"
        onClick={goHome}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            goHome();
          }
        }}
      >
        LENS
      </h1>

      <section className="hero-copy" aria-hidden={!hero}>
        <p className="hero-copy__text">
          Lens reads your day through the book you&apos;re actually reading. Pick a volume off the
          shelf, describe what&apos;s happening, and get a reflection grounded in that book&apos;s own
          principles — never invented advice.
        </p>
        <button type="button" className="hero-scroll" onClick={onExitHero} tabIndex={hero ? 0 : -1}>
          <span className="hero-scroll__label">Scroll to browse the shelf</span>
          <span className="hero-scroll__arrow" aria-hidden="true" />
        </button>
      </section>

      <section className="browse-caption" aria-hidden={isFocused}>
        <p className="eyebrow">
          <span>{String(activeIndex + 1).padStart(2, "0")}</span>
          <span className="eyebrow__line" />
          <span>{String(catalog.length).padStart(2, "0")}</span>
        </p>
        <h2>{activeBook.shortTitle}</h2>
        <p className="browse-caption__author">{activeBook.author}</p>
        <button
          type="button"
          className="inspect-button"
          disabled={isFocused}
          onClick={() => engineRef.current?.focusBook(activeIndex)}
          aria-label={`Inspect ${activeBook.title}`}
        >
          <span>Inspect volume</span>
          <span aria-hidden="true">↗</span>
        </button>
      </section>

      <button
        type="button"
        className="shelf-arrow shelf-arrow--left"
        aria-label="Previous book"
        disabled={isFocused || activeIndex === 0}
        onClick={() => engineRef.current?.browseBy(-1)}
      >
        <ArrowIcon direction="left" />
      </button>
      <button
        type="button"
        className="shelf-arrow shelf-arrow--right"
        aria-label="Next book"
        disabled={isFocused || activeIndex === catalog.length - 1}
        onClick={() => engineRef.current?.browseBy(1)}
      >
        <ArrowIcon direction="right" />
      </button>

      <nav className="shelf-index" aria-label="Shelf position">
        <div className="shelf-index__ticks">
          {catalog.map((book, index) => (
            <button
              key={book.id}
              type="button"
              className={index === activeIndex ? "is-active" : ""}
              aria-label={`Browse to ${book.title}`}
              aria-current={index === activeIndex ? "true" : undefined}
              disabled={isFocused}
              onClick={() => engineRef.current?.browseTo(index)}
            >
              <span />
            </button>
          ))}
        </div>
        <div className="input-hint" aria-hidden="true">
          <span>DRAG</span>
          <i />
          <span>SCROLL</span>
          <i />
          <span>ARROW KEYS</span>
        </div>
      </nav>

      <aside
        className="book-details"
        aria-hidden={!isFocused}
        aria-label={selectedBook ? `Details for ${selectedBook.title}` : "Book details"}
      >
        {selectedBook ? (
          <div className="book-details__inner">
            <button
              type="button"
              className="back-button"
              onClick={() => engineRef.current?.returnToShelf()}
            >
              <ArrowIcon direction="left" />
              <span>Return to shelf</span>
            </button>

            <div className="book-details__position">
              <span>{String(selectedIndex! + 1).padStart(2, "0")}</span>
              <span>{String(catalog.length).padStart(2, "0")}</span>
            </div>

            <div className="book-details__copy">
              <div className="book-details__intro">
                <p className="eyebrow">CORE THESIS</p>
                <h2>{selectedBook.title}</h2>
                <p className="book-details__author">{selectedBook.author}</p>
                <p className="book-details__description">{selectedBook.description}</p>
                <p className="book-details__selected">
                  {selectedBook.id === bookId
                    ? "Selected for analysis"
                    : "Selecting this book…"}
                </p>
              </div>

              {children}
            </div>

            <div className="focus-controls" aria-label="Inspection controls">
              <span>Drag to orbit</span>
              <span>Pinch or scroll to zoom</span>
              <button type="button" onClick={() => engineRef.current?.resetFocusView()}>
                Reset view
              </button>
            </div>
          </div>
        ) : null}
      </aside>

      <div className="experience-status" role="status" aria-live="polite">
        <span className="experience-status__dot" />
        <span>{status}</span>
      </div>

      <div className="loading-screen" aria-hidden={ready}>
        <div className="loading-screen__mark">
          <span />
          <span />
          <span />
        </div>
        <p>Assembling {catalog.length} {catalog.length === 1 ? "volume" : "volumes"}</p>
      </div>

      <p className="site-footer">made with love ❤️ by @bhavyadeep</p>
    </div>
  );
}
