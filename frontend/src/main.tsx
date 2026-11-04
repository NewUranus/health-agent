import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import AdminApp from "./admin/AdminApp";
import { AuthGate } from "./auth/AuthGate";
import "./styles.css";

const adminPath = location.pathname === "/admin";
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthGate adminPath={adminPath}>{adminPath ? <AdminApp /> : <App />}</AuthGate>
  </StrictMode>,
);
