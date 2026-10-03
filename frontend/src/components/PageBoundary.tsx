import { Component, type ReactNode } from "react";

export default class PageBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return (
        <main className="page">
          <div className="alert-banner error" role="alert">
            页面加载失败，请刷新后重试。未保存的编辑内容需要重新输入。
            <button
              className="btn-outline"
              onClick={() => window.location.reload()}
            >
              刷新页面
            </button>
          </div>
        </main>
      );
    }
    return this.props.children;
  }
}
