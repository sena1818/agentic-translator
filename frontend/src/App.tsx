import { lazy, Suspense } from "react";
import { ConfigProvider, App as AntApp } from "antd";
import zhCN from "antd/locale/zh_CN";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { HashRouter, NavLink, Routes, Route } from "react-router-dom";
import TranslationPage from "./components/TranslationPage";
import PageBoundary from "./components/PageBoundary";
import { getSystemState } from "./services/api";

const GlossaryManager = lazy(() => import("./components/GlossaryManager"));
const TaskDetail = lazy(() => import("./components/TaskDetail"));
const ReaderPage = lazy(() => import("./components/ReaderPage"));
const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
});

function SystemStatus() {
  const { data, isError } = useQuery({
    queryKey: ["system"],
    queryFn: getSystemState,
    refetchInterval: 10000,
  });
  const online = !isError && !!data?.workers_online;
  return (
    <div className="nav-meta" role="status">
      <span className={`online-dot ${online ? "" : "offline"}`} />
      {isError
        ? "连接失败"
        : !data
          ? "正在连接"
          : online
            ? `${data.workers_online} 个 Worker 在线`
            : "Worker 离线"}
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ConfigProvider
        locale={zhCN}
        theme={{
          token: {
            colorPrimary: "#2d4a3e",
            colorBgContainer: "#ffffff",
            borderRadius: 5,
            fontFamily: "system-ui, sans-serif",
            colorText: "#1a1816",
            colorTextSecondary: "#5c5751",
            colorBorder: "#e4e0d8",
            colorBgElevated: "#ffffff",
          },
        }}
      >
        <AntApp>
          <HashRouter>
            <div className="app">
              <nav className="topnav" aria-label="主导航">
                <NavLink className="nav-logo" to="/">
                  <span className="wordmark">Translator</span>
                  <span className="edition">LLM Edition</span>
                </NavLink>
                <div className="nav-tabs">
                  <NavLink
                    className={({ isActive }) =>
                      `nav-tab ${isActive ? "active" : ""}`
                    }
                    to="/"
                    end
                  >
                    翻译
                  </NavLink>
                  <NavLink
                    className={({ isActive }) =>
                      `nav-tab ${isActive ? "active" : ""}`
                    }
                    to="/glossary"
                  >
                    术语表
                  </NavLink>
                </div>
                <SystemStatus />
              </nav>
              <PageBoundary>
                <Suspense
                  fallback={
                    <div className="page" role="status">
                      加载中…
                    </div>
                  }
                >
                  <Routes>
                    <Route path="/" element={<TranslationPage />} />
                    <Route path="/tasks/:taskId" element={<TaskDetail />} />
                    <Route
                      path="/tasks/:taskId/read"
                      element={<ReaderPage />}
                    />
                    <Route path="/glossary" element={<GlossaryManager />} />
                    <Route
                      path="*"
                      element={
                        <div className="page">
                          页面不存在。<NavLink to="/">返回任务列表</NavLink>
                        </div>
                      }
                    />
                  </Routes>
                </Suspense>
              </PageBoundary>
            </div>
          </HashRouter>
        </AntApp>
      </ConfigProvider>
    </QueryClientProvider>
  );
}
