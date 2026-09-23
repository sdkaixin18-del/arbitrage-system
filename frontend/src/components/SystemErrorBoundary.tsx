import React from "react";
import { Alert, Button } from "antd";
import { reportRuntimeError } from "../runtimeTelemetry";

interface State {
  error: Error | null;
}

export default class SystemErrorBoundary extends React.Component<React.PropsWithChildren, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    void reportRuntimeError({
      event: "react_render_error",
      message: error.message || "页面组件渲染失败",
      errorType: error.name,
      stack: error.stack,
      details: { componentStack: info.componentStack }
    });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div style={{ padding: 24 }}>
        <Alert
          type="error"
          showIcon
          message="页面显示异常，错误已自动记录"
          description={this.state.error.message}
          action={<Button onClick={() => window.location.reload()}>重新加载</Button>}
        />
      </div>
    );
  }
}
