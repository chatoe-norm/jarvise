import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode };
type State = { error: Error | null };

/** Keeps one broken card from blanking the whole dashboard. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Dashboard render error", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="m-4 rounded-xl border border-[var(--color-danger)]/40 bg-[var(--color-card)] p-5 text-sm">
        <p className="font-semibold text-[var(--color-danger)]">หน้านี้แสดงผลไม่ได้</p>
        <p className="mt-1 text-[var(--color-muted)]">
          ข้อมูลจากเซิร์ฟเวอร์มีรูปแบบที่ไม่คาดไว้ ระบบเทรดยังทำงานตามปกติ ลองโหลดใหม่อีกครั้ง
        </p>
        <p className="mt-2 font-mono text-xs text-[var(--color-muted)]">{this.state.error.message}</p>
        <button
          type="button"
          className="mt-4 h-11 rounded-md border border-[var(--color-border)] px-4 font-semibold"
          onClick={() => window.location.reload()}
        >
          โหลดใหม่
        </button>
      </div>
    );
  }
}
