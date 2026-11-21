import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

import "../i18n";

// jsdom doesn't implement <dialog>'s modal methods (Modal/ConfirmActionDialog
// both use them) — a minimal polyfill so components using them are testable.
if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  };
}

afterEach(() => {
  window.localStorage.clear();
});
