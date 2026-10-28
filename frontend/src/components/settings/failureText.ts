/** A failure is always shown with some text: an empty message must never read as "nothing happened". */
export function failureText(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error) return error;
  return "the server gave no reason (check the browser console and the Condor log)";
}
