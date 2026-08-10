import type { Book } from "./api";

/**
 * A point-in-time snapshot of the real, already-reviewed book catalog --
 * NOT placeholder/fabricated content. Lets the 3D shelf render and be fully
 * explorable (drag, scroll, arrow keys, reading covers) the instant the
 * page loads, with zero backend round-trip, instead of sitting on a blank
 * "Pulling your shelf together…" screen until `listBooks()` resolves --
 * which on Render's free tier can mean up to ~60s if the backend was
 * asleep (see docs/DEPLOYMENT.md and Architecture.md's "Optimistic shelf
 * render" addendum).
 *
 * `page.tsx` renders this immediately on mount, then fetches the live list
 * in the background and silently replaces it once that resolves -- a user
 * who never interacts with the backend-dependent parts (submitting a
 * situation) never has to wait on anything. Only `handleSubmit` actually
 * blocks on live data, since it must (it writes to the real user's account).
 *
 * Regenerate by hand after adding/editing/removing a book:
 *   curl -s $NEXT_PUBLIC_API_BASE/api/books | jq '[.[] | {book_id, title, author, core_thesis, tone, review_status}]'
 * Going stale here is low-stakes -- worst case a user briefly sees last
 * week's catalog before it reconciles to live data a moment later.
 */
export const BOOK_CATALOG_FALLBACK: Book[] = [
  {
    book_id: "12-rules-for-life",
    title: "12 Rules for Life",
    author: "Jordan B. Peterson",
    core_thesis:
      "This book presents a comprehensive guide for living a meaningful life by applying 12 rules that promote personal growth, responsibility, and self-awareness. The principles outlined in the book emphasize the importance of treating oneself with kindness, setting boundaries, and taking responsibility for one's actions. It also highlights the need to balance order and chaos, cultivate resilience, and prioritize meaning over expediency. By following these rules, individuals can develop a stronger sense of purpose, improve their relationships, and navigate life's challenges with greater confidence and wisdom.",
    tone: "philosophical",
    review_status: "human_reviewed",
  },
  {
    book_id: "atomic-habits",
    title: "Atomic Habits",
    author: "James Clear",
    core_thesis:
      "Atomic habits, as outlined in James Clear's book, argue that small, consistent changes can lead to significant improvements over time. By focusing on systems rather than goals, and using strategies such as habit stacking, implementation intentions, and environmental cues, individuals can create a framework for lasting change. The key is to make progress visible, track habits, and automate good behaviors, while avoiding bad habits and making small changes that add up to big results.",
    tone: "pragmatic",
    review_status: "human_reviewed",
  },
  {
    book_id: "never-split-the-difference",
    title: "Never Split the Difference",
    author: "Chris Voss",
    core_thesis:
      "Effective negotiation is not about using logic and power to get what you want, but rather about understanding human psychology and emotions to build trust and create mutually beneficial solutions. By establishing trust through empathy, using tactical empathy, and focusing on interests rather than positions, negotiators can create a safe and collaborative environment that fosters creative problem-solving and leads to successful outcomes. This approach involves active listening, calibrated questions, and a deep understanding of the counterpart's perspective, allowing negotiators to separate emotions from problem-solving and find solutions that satisfy both parties' needs.",
    tone: "pragmatic",
    review_status: "human_reviewed",
  },
  {
    book_id: "rich-dad-poor-dad",
    title: "Rich Dad Poor Dad",
    author: "Robert Kiyosaki",
    core_thesis:
      "Financial success is not solely dependent on hard work, but rather on developing a strong money mindset, acquiring financial education, and taking calculated risks to build wealth through assets and passive income. By prioritizing financial intelligence, entrepreneurship, and risk-taking, individuals can break free from the constraints of a traditional income and achieve financial freedom. This requires a shift in perspective, embracing change, and being willing to challenge conventional wisdom and take bold action to create a life of wealth and prosperity.",
    tone: "pragmatic",
    review_status: "human_reviewed",
  },
  {
    book_id: "48-laws-of-power",
    title: "The 48 Laws of Power",
    author: "Robert Greene",
    core_thesis:
      "The pursuit of power requires a delicate balance between manipulation, strategy, and self-preservation. To achieve and maintain power, one must master the art of deception, using tactics such as misdirection, flattery, and concealment to create an illusion of superiority. This involves understanding human psychology, exploiting people's desires and weaknesses, and adapting to changing circumstances. Effective power players must also cultivate a sense of mystery, control their emotions, and be willing to take calculated risks. Ultimately, the key to power lies in the ability to navigate complex webs of relationships, manipulate perceptions, and maintain a strong sense of autonomy and self-preservation.",
    tone: "pragmatic",
    review_status: "human_reviewed",
  },
  {
    book_id: "six-pillars-of-self-esteem",
    title: "The Six Pillars of Self-Esteem",
    author: "Nathaniel Branden",
    core_thesis:
      "This book argues that self-esteem is a fundamental human need that can be developed and strengthened through conscious practices, including self-awareness, self-responsibility, and personal growth. By integrating principles from psychology, philosophy, and spirituality, The Six Pillars of Self-Esteem provides a comprehensive framework for building resilience, confidence, and inner strength, enabling individuals to navigate life's challenges with courage, purpose, and fulfillment.",
    tone: "pragmatic",
    review_status: "human_reviewed",
  },
  {
    book_id: "subtle-art-of-not-giving-a-fck",
    title: "The Subtle Art of Not Giving a F*ck",
    author: "Mark Manson",
    core_thesis:
      "Life's purpose is not solely focused on achieving happiness, but rather on living with what we have and accepting the downsides. By giving up attachment to certain outcomes or expectations, we can simplify our lives and focus on what truly matters. This requires taking responsibility for our choices, accepting failure as a necessary part of growth, and recognizing that pain and loss are inevitable parts of life. By embracing uncertainty, imperfection, and vulnerability, we can develop a greater sense of self-worth and live more authentic lives. Ultimately, the key to happiness lies not in achieving greatness, but in living with intention and purpose.",
    tone: "pragmatic",
    review_status: "human_reviewed",
  },
];
