# Prompt: Design the Architecture for "Lens" — A Book-Grounded Life Analysis App

> Paste everything below into a fresh Claude conversation to get a complete architecture proposal.

---

## Role

You are a senior software architect and AI systems designer with deep experience in retrieval-augmented generation (RAG) systems, personal analytics products, and LLM orchestration. You reason in terms of data flow, failure modes, and testable outputs — not feature lists or marketing language.

## Goal

Design the full technical architecture for a web app, codename **Lens**, with this measurable objective:

A user can (1) pick a book from a curated catalogue, (2) log their daily actions, and (3) receive an analysis of that day written through the lens of that book's core principles, plus concrete suggestions for tomorrow — all without the system ever storing or reproducing copyrighted book text.

**Definition of done:** your answer contains all seven sections listed under "Required Output," each one internally consistent with the others (the schemas support the API, the API supports the retrieval pipeline, etc.), with zero unresolved contradictions and zero violations of the constraints below.

## Constraints (non-negotiable)

1. **No verbatim book storage.** Never design a component that ingests, stores, or serves full copyrighted book text — in a database, vector store, cache, or prompt. "Book knowledge" in this system means structured, human-reviewed *principle extractions* (thesis, key ideas, frameworks, one-line summaries), not the book itself. Short fair-use quotes (a few lines, cited) are the only exception.
2. **Every legal/licensing/business decision is flagged, not assumed.** If a design choice requires a licensing deal, a paid API, or a legal read, put it in the "Needs Me" list instead of silently picking an answer.
3. **User data is sensitive.** Daily logs are personal-life data. Every data flow must show where it's stored, whether it ever leaves the user's control, and how it's protected at rest and in transit.

## Guidelines (bend these only with a stated reason)

- Prefer a RAG-style pipeline over fine-tuning — the catalogue grows and changes, and knowledge should update without retraining anything.
- Treat "retrieve relevant principles" and "synthesize analysis" as two separate, independently testable steps, not one opaque prompt.
- Make the book catalogue a data structure, not per-book code, so adding book #51 doesn't need a code change.
- Prefer boring, proven infrastructure unless a novel choice buys a measurable, stated benefit.

## Context

Lens is a personal-development web app. Core loop: user selects a book → logs today's actions/journal → app retrieves the principles from that book most relevant to today's entries → an LLM step produces (a) an analysis of the day through that lens, and (b) 1–3 concrete suggested actions for tomorrow, grounded in specific ideas from the book → user marks suggestions done/skipped, which feeds tomorrow's context. Over weeks, the user sees trends against that book's framework (e.g. "consistency" for Atomic Habits, "control vs. non-control" for a Stoic text).

## Data to design around

Book catalogue entry (draft — refine as needed):
```json
{
  "book_id": "atomic-habits",
  "title": "Atomic Habits",
  "author": "James Clear",
  "core_thesis": "one paragraph, human-written",
  "principles": [
    {
      "id": "identity-based-habits",
      "name": "Identity-based habits",
      "summary": "one paragraph, human-written, no verbatim text",
      "source_chapter": "ch. 2",
      "applies_to_tags": ["habit-formation", "self-image"]
    }
  ],
  "tone": "pragmatic",
  "review_status": "human_reviewed | pending_review"
}
```

Daily log entry (draft):
```json
{
  "user_id": "...",
  "date": "2026-07-01",
  "chosen_book_id": "atomic-habits",
  "entries": [
    {"time": "07:30", "action": "went for a run", "category": "health"}
  ],
  "mood": "optional, 1-5"
}
```

## Required Output — exactly these seven sections, in this order

1. **System Architecture Diagram** — Mermaid diagram + short prose walkthrough. Cover frontend, API layer, primary DB, vector store, LLM orchestration layer, and the book-ingestion pipeline as distinct labeled components with directional arrows.
2. **Book Ingestion Pipeline** — how a new book gets from "someone suggests it" to "searchable principles in the catalogue." Name exactly where a human reviews the extracted principles and why that step can't be skipped.
3. **Retrieval + Analysis Engine** — how a day's log entries get matched to the chosen book's principles (embedding search? tag matching? both?), and how the final analysis + suggestions get generated. Include the actual prompt template skeleton (with placeholders) you'd send the LLM, and name one self-check/verification step the system runs before showing the result to the user.
4. **Data Model** — final schemas for books, principles, users, daily logs, suggestions, and streak/progress tracking.
5. **Tech Stack** — one line of justification per component, plus the one alternative you rejected for each and why.
6. **Evaluation Plan** — a concrete plan for a 5–10 case test set that checks the analysis engine's output is actually faithful to the book's principles (not hallucinated), before this ships to real users.
7. **Needs Me** — every business, legal, or licensing decision this design depends on that you did not make for me.

## Output format

- Markdown, the seven headings above verbatim and in order.
- Mermaid for diagrams.
- Bullets over paragraphs wherever the content allows it.
- Stop after "Needs Me" — no summary or conclusion section.

## Before answering

Confirm internally that no part of your design stores or serves full copyrighted book text. If any component would require that, redesign it instead of just noting the violation.
