/**
 * Minimal pub/sub so non-React modules (like the api-client fetch helper)
 * can surface a toast without every individual call site needing to know
 * about auth-specific error handling. AuthGate wires this up to the real
 * useToast() hook once, near the root of the app.
 */

type NotifyMessage = { title: string; description?: string };
type Listener = (message: NotifyMessage) => void;

let listener: Listener | null = null;

export function setNotifyListener(fn: Listener | null) {
  listener = fn;
}

export function notify(message: NotifyMessage) {
  listener?.(message);
}
