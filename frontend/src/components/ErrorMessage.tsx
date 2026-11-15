import { ApiError } from "../api/client";

/** One error, as text for the user. */
export function ErrorMessage({ error }: { error: unknown }) {
  if (!error) return null;
  const text =
    error instanceof ApiError
      ? `${error.message} (${error.code})`
      : error instanceof Error
        ? error.message
        : "Unknown error";
  return (
    <p className="error-box" role="alert">
      {text}
    </p>
  );
}
