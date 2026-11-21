export interface PathCrumb {
  label: string;
  path: string;
}

/** Splits an absolute path into clickable breadcrumb segments, each
 * carrying the full path up to and including itself — shared by
 * `FileBrowserPanel` (on-demand PBS browsing) and `FileIndexBrowserPanel`
 * (browsing the persistent index), both of which render the same
 * root-plus-segments breadcrumb over an absolute, "/"-separated path.
 */
export function pathSegments(path: string): PathCrumb[] {
  const parts = path.split("/").filter(Boolean);
  const crumbs: PathCrumb[] = [];
  let current = "";
  for (const part of parts) {
    current += `/${part}`;
    crumbs.push({ label: part, path: current });
  }
  return crumbs;
}
