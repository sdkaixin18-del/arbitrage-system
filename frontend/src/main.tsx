import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import "antd/dist/reset.css";
import "./styles.css";
import App from "./App";
import SystemErrorBoundary from "./components/SystemErrorBoundary";
import { installRuntimeTelemetry } from "./runtimeTelemetry";

installRuntimeTelemetry();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      staleTime: 60 * 1000,
      gcTime: 10 * 60 * 1000,
      retry: 1
    }
  }
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#1f6f5b",
          borderRadius: 6,
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei", sans-serif'
        }
      }}
    >
      <QueryClientProvider client={queryClient}>
        <SystemErrorBoundary>
          <App />
        </SystemErrorBoundary>
      </QueryClientProvider>
    </ConfigProvider>
  </React.StrictMode>
);
