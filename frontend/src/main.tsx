import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import AdminApp from "./admin/AdminApp";
import "./styles.css";

createRoot(document.getElementById("root")!).render(<StrictMode>{location.pathname === "/admin" ? <AdminApp /> : <App />}</StrictMode>);
