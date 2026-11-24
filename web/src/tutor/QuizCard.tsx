import { useState } from "react";
import type { QuizInterrupt } from "./chat";

/** A quiz interrupt: multiple choice (answers are the option text) and short answers. All must be answered. */
export function QuizCard({
  value,
  busy,
  onSubmit,
}: {
  value: QuizInterrupt;
  busy: boolean;
  onSubmit: (resume: { answers: Record<string, string> }) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const complete = value.questions.every((q) => (answers[q.id] ?? "").trim() !== "");
  return (
    <form
      className="card stack"
      aria-label="Quiz"
      onSubmit={(e) => {
        e.preventDefault();
        if (complete && !busy) onSubmit({ answers });
      }}
    >
      <h3>Quiz</h3>
      {value.questions.map((q, i) => (
        <fieldset key={q.id} className="question" disabled={busy}>
          <legend>
            {i + 1}. {q.prompt}
          </legend>
          {q.type === "mcq" ? (
            (q.options ?? []).map((opt) => (
              <label key={opt} className="option">
                <input type="radio" name={`q-${q.id}`} value={opt} checked={answers[q.id] === opt} onChange={() => setAnswers({ ...answers, [q.id]: opt })} />
                <span className="option-text">{opt}</span>
              </label>
            ))
          ) : (
            <textarea rows={3} aria-label={`Answer ${i + 1}`} value={answers[q.id] ?? ""} onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })} />
          )}
        </fieldset>
      ))}
      <button type="submit" disabled={busy || !complete}>
        Submit answers
      </button>
    </form>
  );
}
