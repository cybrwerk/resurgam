import { afterEach, describe, expect, it, vi } from "vitest";

import { API_BASE_URL, startBrowserDownload } from "./client";

describe("startBrowserDownload", () => {
  afterEach(() => vi.restoreAllMocks());

  it("lets the browser navigate to the ticket URL (streams to disk, no Blob) and cleans up the link", () => {
    const clicked: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this.href);
    });

    startBrowserDownload("/file-restore/jobs/job-1/download?ticket=abc");

    expect(clicked).toEqual([`${API_BASE_URL}/file-restore/jobs/job-1/download?ticket=abc`]);
    expect(document.querySelector("a")).toBeNull();
  });

  it("marks the link as a download, so an error answer never replaces the app", () => {
    const links: { download: string; target: string }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      links.push({ download: this.getAttribute("download") ?? "<none>", target: this.target });
    });

    startBrowserDownload("/file-restore/jobs/job-1/download?ticket=abc");

    expect(links[0].download).toBe("");
    // The test API base (localhost:8000) is another origin than jsdom's page: `download` is
    // ignored there, so the link opens in a new tab instead of navigating this one.
    const crossOrigin = new URL(API_BASE_URL, window.location.href).origin !== window.location.origin;
    expect(links[0].target).toBe(crossOrigin ? "_blank" : "");
  });
});
