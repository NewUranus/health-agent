import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom 没有实现 scrollIntoView
Element.prototype.scrollIntoView = () => {};
afterEach(cleanup);
