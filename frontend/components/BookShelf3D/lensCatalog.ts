import type { Book, BookTone } from "@/app/api";

// The shelf renderer (ShelfEngine / cover-art) is asset-free: every cover is
// drawn procedurally from these fields. Lens books carry no artwork, so each
// book's visual identity is derived deterministically from its tone plus a hash
// of its identity -- the same book always looks the same, and the shelf works
// for any number of books rather than a fixed pack.

export type BookMotif =
  | "lattice"
  | "corrosion"
  | "efficiency"
  | "network"
  | "boom"
  | "organization"
  | "schematic"
  | "flight"
  | "circuit"
  | "orbit"
  | "branches"
  | "wave"
  | "runner"
  | "gather"
  | "maze"
  | "fracture"
  | "continuum"
  | "windows"
  | "steps";

export type CatalogBook = {
  id: string;
  title: string;
  shortTitle: string;
  author: string;
  description: string;
  quote: string;
  quoteBy: string;
  cover: string;
  accent: string;
  ink: string;
  motif: BookMotif;
  height: number;
  thickness: number;
  coverImage?: string;
  living?: boolean;
};

type Palette = { cover: string; accent: string; ink: string };

const PALETTES: Record<BookTone, Palette[]> = {
  pragmatic: [
    { cover: "#3d4f57", accent: "#c8a24a", ink: "#f0e8d8" },
    { cover: "#45514a", accent: "#d0b05e", ink: "#f2ece0" },
    { cover: "#4e4a3c", accent: "#c9a227", ink: "#f1ead9" },
  ],
  philosophical: [
    { cover: "#453b52", accent: "#c2a56b", ink: "#efe7dc" },
    { cover: "#383a4d", accent: "#b9b2a0", ink: "#eee8dd" },
    { cover: "#52414d", accent: "#caa878", ink: "#f1e9de" },
  ],
  spiritual: [
    { cover: "#7a4a35", accent: "#e0bf7a", ink: "#f7efe1" },
    { cover: "#845f36", accent: "#ecd39a", ink: "#f8f1e3" },
    { cover: "#67453f", accent: "#d9b071", ink: "#f4ece0" },
  ],
  scientific: [
    { cover: "#33555a", accent: "#9fc2b4", ink: "#eef2ec" },
    { cover: "#3b4f5e", accent: "#a8bdc8", ink: "#eef1f2" },
    { cover: "#44615b", accent: "#bcd0be", ink: "#f0f3ea" },
  ],
  narrative: [
    { cover: "#65373a", accent: "#dba766", ink: "#f6ebde" },
    { cover: "#75452f", accent: "#e6bd80", ink: "#f8efe0" },
    { cover: "#57353f", accent: "#cf9d73", ink: "#f3e9de" },
  ],
};

const MOTIFS: Record<BookTone, BookMotif[]> = {
  pragmatic: ["efficiency", "organization", "schematic", "steps"],
  philosophical: ["lattice", "continuum", "windows", "orbit"],
  spiritual: ["wave", "branches", "gather", "flight"],
  scientific: ["network", "circuit", "fracture", "maze"],
  narrative: ["runner", "boom", "corrosion", "lattice"],
};

function hash(value: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** First sentence of the thesis, used as the back-cover quote. */
function firstSentence(text: string) {
  const trimmed = text.trim();
  const match = trimmed.match(/^(.{20,150}?[.!?])\s/);
  if (match) return match[1];
  return trimmed.length > 150 ? `${trimmed.slice(0, 147)}...` : trimmed;
}

/** Long titles are unreadable on a spine, so drop the subtitle before truncating. */
function shortenTitle(title: string) {
  const colon = title.indexOf(":");
  if (colon > 8 && colon <= 42) return title.slice(0, colon).trim();
  if (title.length <= 42) return title;
  const cut = title.slice(0, 42);
  const space = cut.lastIndexOf(" ");
  return `${(space > 20 ? cut.slice(0, space) : cut).trim()}…`;
}

export function toCatalogBook(book: Book): CatalogBook {
  const seed = hash(`${book.book_id}|${book.title}|${book.author}`);
  const tone: BookTone = PALETTES[book.tone] ? book.tone : "pragmatic";
  const palette = PALETTES[tone];
  const motifs = MOTIFS[tone];

  return {
    id: book.book_id,
    title: book.title,
    shortTitle: shortenTitle(book.title),
    author: book.author,
    description: book.core_thesis,
    quote: firstSentence(book.core_thesis),
    quoteBy: book.author,
    ...palette[seed % palette.length],
    motif: motifs[(seed >>> 8) % motifs.length],
    height: 1.92 + (((seed >>> 12) % 29) * 0.01),
    thickness: 0.16 + (((seed >>> 20) % 15) * 0.01),
  };
}

/** Tallest-first, matching the editorial arrangement of the original shelf. */
export function toCatalog(books: Book[]): CatalogBook[] {
  return books.map(toCatalogBook).sort((a, b) => b.height - a.height);
}
