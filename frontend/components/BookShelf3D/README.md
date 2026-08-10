# BookShelf3D

A 3D book picker: browse Lens books as clothbound hardcovers on a walnut shelf,
pull one forward to inspect it, and select it as the active book.

## Origin

Adapted from **"The Complete Shelf"** by Mint, MIT licensed — see `LICENSE.mint`.

- Source: <https://github.com/mintdotgg/mint-playground/tree/main/experiences/complete-shelf>
- Demo: <https://play.mint.gg/complete-shelf>

The upstream experience ships **no binary assets** (`asset-manifest.json` is
empty); every cover is drawn procedurally onto a 2D canvas. That is why this
works without a texture pipeline or Mint MCP at runtime.

### What was changed

- **Catalog replaced.** Upstream ships a fixed 19-book catalog of Stripe Press
  titles. Those are *not* covered by its MIT grant (see the upstream
  `THIRD_PARTY_NOTICES.md`), and Lens's book list is dynamic anyway. `catalog.ts`
  was dropped entirely and replaced by `lensCatalog.ts`.
- **Stripe asset archive removed.** The optional `loadStripeAssets` /
  `loadStripeBook` / `textureFor` paths, `stripe-assets.ts`, `stripe-foil.ts`,
  and the `OBJLoader` import are gone. They were disabled upstream by default.
- **Fonts.** Upstream bundles Newsreader/Inter; Lens uses system serif/sans.
- **Wrapper.** `ProgressLibrary.tsx` became `BookShelf3D.tsx`, taking
  `books` / `bookId` / `onSelect` props instead of importing a module-level
  catalog, and the "official link" was replaced with a select action.
- **CSS.** Ported into `app/globals.css`, scoped under `.shelf-experience`
  (upstream owned the whole page). The position-marker grid no longer hardcodes
  19 columns.

## Files

| File | Origin |
| --- | --- |
| `ShelfEngine.ts` | upstream, Stripe paths removed |
| `cover-art.ts` | upstream, fonts changed |
| `book-motion.ts` | upstream, unmodified |
| `lensCatalog.ts` | Lens |
| `site-config.ts` | Lens |
| `BookShelf3D.tsx` | Lens, based on upstream `ProgressLibrary.tsx` |

## How a book gets its look

`lensCatalog.ts` maps a Lens `Book` to the `CatalogBook` shape the renderer
expects. There is no cover art in the Lens schema, so appearance is derived
deterministically:

- `tone` selects a colour palette (cloth / foil / ink) and a family of foil motifs.
- An FNV-1a hash of `book_id|title|author` picks the variant within that family,
  plus spine thickness and height.

The same book therefore always renders identically, and the shelf scales to any
number of books rather than a fixed pack.
