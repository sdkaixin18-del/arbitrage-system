import { createContext, useContext, useRef } from "react";
import type { MutableRefObject } from "react";
import { Typography } from "antd";
import { NavLink, Outlet } from "react-router-dom";
import { DashboardOutlined, SlidersOutlined, LinkOutlined } from "@ant-design/icons";
import "./astroWorkspace.css";

const RulesDraftContext = createContext<MutableRefObject<Record<string, unknown> | null> | null>(null);

export function useAstroRulesDraft() {
  const draft = useContext(RulesDraftContext);
  if (!draft) throw new Error("Astro rules must be rendered within the Astro workspace");
  return draft;
}

export default function AstroAutoCardLayout() {
  const draft = useRef<Record<string, unknown> | null>(null);
  return (
    <RulesDraftContext.Provider value={draft}>
      <div className="astro-workspace">
        <header className="astro-workspace-header">
          <Typography.Title level={2}>Astro 建卡</Typography.Title>
          <nav className="astro-workspace-tabs" aria-label="Astro 建卡二级导航">
            <NavLink to="/astro/status" className={({ isActive }) => isActive ? "is-active" : ""}><DashboardOutlined /> 运行状态</NavLink>
            <NavLink to="/astro/rules" className={({ isActive }) => isActive ? "is-active" : ""}><SlidersOutlined /> 套利规则</NavLink>
            <NavLink to="/astro/dex" className={({ isActive }) => isActive ? "is-active" : ""}><LinkOutlined /> DEX 配置</NavLink>
          </nav>
        </header>
        <Outlet />
      </div>
    </RulesDraftContext.Provider>
  );
}
