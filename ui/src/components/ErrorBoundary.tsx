import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  /** What to isolate. A throw anywhere inside renders `fallback` instead of
      unmounting the whole React tree (which shows as a blank dock). */
  children: ReactNode;
  /** Rendered in place of the subtree when it throws. */
  fallback: ReactNode;
  /** Bump to retry after the offending state changes (e.g. a new chat). */
  resetKey?: unknown;
}

interface State {
  failed: boolean;
}

/**
 * A render error in ONE message (a malformed replayed proposal, a schema
 * drift) must not blank the entire dock. This catches it, shows a small
 * inline fallback, and keeps the rest of the UI alive. Class component because
 * React error boundaries have no hook form. Dogfood 2026-07-12: a bad
 * proposal payload white-screened the whole app.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surfaces in the webview console + backend.log territory; not fatal.
    console.error("cwyc: render error contained by boundary", error, info.componentStack);
  }

  componentDidUpdate(prev: Props): void {
    if (prev.resetKey !== this.props.resetKey && this.state.failed) {
      this.setState({ failed: false });
    }
  }

  render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
