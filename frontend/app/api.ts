const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export type BookTone = "pragmatic" | "philosophical" | "spiritual" | "scientific" | "narrative";

export type Book = {
  book_id: string;
  title: string;
  author: string;
  core_thesis: string;
  tone: BookTone;
  review_status: string;
};

export type Suggestion = {
  suggestion_id: string;
  principle_id: string;
  text: string;
  explanation: string;
  status: string;
};

export type Analysis = {
  analysis_id: string;
  log_id: string;
  reflection: string;
  verification_status: string;
  retrieved_principle_ids: string[];
};

export type Principle = { principle_id: string; name: string };

export type Streak = { current_streak_days: number; longest_streak_days: number };
export type UserRec = { user_id: string; timezone: string; active_book_id: string | null };

export type LogEntry = { time: string; action: string; category: string };

export type DailyLog = {
  log_id: string;
  user_id: string;
  date: string;
  chosen_book_id: string;
  entries: LogEntry[];
  mood: number | null;
};

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status}: ${detail}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export async function getOrCreateUser(): Promise<UserRec> {
  const stored = typeof window !== "undefined" ? localStorage.getItem("lens_user_id") : null;
  if (stored) {
    try {
      return await request<UserRec>(`/api/users/${stored}`);
    } catch {
      // stored id no longer valid (e.g. DB reset) -- fall through and create a new one
    }
  }
  const user = await request<UserRec>("/api/users", { method: "POST", body: JSON.stringify({}) });
  localStorage.setItem("lens_user_id", user.user_id);
  return user;
}

export const listBooks = () => request<Book[]>("/api/books");

export const getPrinciple = (principleId: string) => request<Principle>(`/api/principles/${principleId}`);

export const setActiveBook = (userId: string, bookId: string) =>
  request<UserRec>(`/api/users/${userId}/active-book?book_id=${encodeURIComponent(bookId)}`, { method: "PUT" });

export const createDailyLog = (payload: {
  user_id: string;
  date: string;
  chosen_book_id: string;
  entries: LogEntry[];
  mood?: number;
}) => request<DailyLog>("/api/daily-logs", { method: "POST", body: JSON.stringify(payload) });

export const createAnalysis = (logId: string) =>
  request<Analysis>("/api/analyses", { method: "POST", body: JSON.stringify({ log_id: logId }) });

export const listSuggestions = (analysisId: string) =>
  request<Suggestion[]>(`/api/suggestions?analysis_id=${analysisId}`);

export const updateSuggestion = (suggestionId: string, status: "done" | "skipped") =>
  request<Suggestion>(`/api/suggestions/${suggestionId}`, { method: "PUT", body: JSON.stringify({ status }) });

export const getStreak = (userId: string, bookId: string) =>
  request<Streak>(`/api/streaks?user_id=${userId}&book_id=${encodeURIComponent(bookId)}`);
