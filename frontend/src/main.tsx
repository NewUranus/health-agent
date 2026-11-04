import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import AdminApp from "./admin/AdminApp";
import { AuthGate } from "./auth/AuthGate";
import "./styles.css";

createRoot(document.getElementById("root")!).render(<StrictMode>{location.pathname === "/admin" ? <AdminApp /> : <AuthGate><App /></AuthGate>}</StrictMode>);
