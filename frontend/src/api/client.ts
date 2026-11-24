export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";

export class ApiError extends Error {
  code: string;
  status: number;
  correlationId?: string;
  details?: unknown;

  constructor(status: number, code: string, message: string, correlationId?: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
    this.details = details;
  }
}

let authToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setAuthToken(token: string | null): void {
  authToken = token;
}

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Use this Bearer token instead of the session (the forced MFA
   * enrollment at login). A 401 then does not log the session out. */
  token?: string;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const bearer = options.token ?? authToken;
  if (bearer) {
    headers.Authorization = `Bearer ${bearer}`;
  }
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: options.method ?? "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  if (response.status === 401 && options.token === undefined) {
    unauthorizedHandler?.();
  }

  if (response.status === 204) {
    return undefined as T;
  }

  let payload: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const body = (payload ?? {}) as { code?: string; message?: string; correlation_id?: string; details?: unknown };
    throw new ApiError(
      response.status,
      body.code ?? "unknown_error",
      body.message ?? response.statusText,
      body.correlation_id,
      body.details,
    );
  }

  return payload as T;
}

async function _throwIfNotOk(response: Response): Promise<void> {
  if (response.ok) return;
  let payload: unknown = null;
  try {
    payload = JSON.parse(await response.text());
  } catch {
    payload = null;
  }
  const body = (payload ?? {}) as { code?: string; message?: string; correlation_id?: string; details?: unknown };
  throw new ApiError(
    response.status,
    body.code ?? "unknown_error",
    body.message ?? response.statusText,
    body.correlation_id,
    body.details,
  );
}

/** Downloads a file response (e.g. a CSV export) and saves it via the
 * browser — `apiRequest` always parses JSON, which doesn't fit a file
 * response. Not usable from an Artifact-style sandbox (script-driven
 * saves are inert there), but this is the real app running in the
 * user's own browser, so a normal `<a download>` click works fine.
 */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const headers: Record<string, string> = {};
  if (authToken) headers.Authorization = `Bearer ${authToken}`;

  const response = await fetch(`${API_BASE_URL}${path}`, { headers });
  if (response.status === 401) unauthorizedHandler?.();
  await _throwIfNotOk(response);

  downloadBlob(await response.blob(), filename);
}

/** POSTs a JSON body and saves the file the server answers with -- for
 * downloads that need a request body, such as an encryption key download
 * with its fresh confirmation (ADR 0031). The file name comes from the
 * server's `Content-Disposition`, `fallbackName` otherwise. Errors are thrown
 * as `ApiError`, like `apiRequest`. */
export async function postDownload(path: string, body: unknown, fallbackName: string): Promise<void> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;

  const response = await fetch(`${API_BASE_URL}${path}`, { method: "POST", headers, body: JSON.stringify(body) });
  if (response.status === 401) unauthorizedHandler?.();
  await _throwIfNotOk(response);

  const disposition = response.headers.get("content-disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  downloadBlob(await response.blob(), match ? match[1] : fallbackName);
}

/** Lets the browser itself download `path` (relative to the API base) by navigating to it -- the
 * response streams straight to disk, unlike `downloadFile`, which buffers the whole body as a Blob
 * in the page first (fine for a CSV, not for a multi-GB ZIP). There is no Authorization header on
 * a navigation, so `path` must carry its own credential (a file-restore download ticket). The
 * server answers with `Content-Disposition: attachment`, so the current page stays put.
 *
 * An error answer (410 expired, 401 ticket timed out) has no attachment header, so a plain
 * navigation would replace the app with raw JSON. Hence the `download` attribute (empty: keep the
 * server's file name): the browser then treats any answer as a download and an error ends as a
 * failed download, never a navigation. Browsers ignore `download` for a cross-origin URL (a dev
 * setup with the API on another port), so there the link opens in a new tab instead -- the tab
 * closes itself once the attachment starts, and an error shows there, not in place of the app. */
export function startBrowserDownload(path: string): void {
  const link = document.createElement("a");
  link.href = `${API_BASE_URL}${path}`;
  link.rel = "noopener";
  link.download = "";
  if (new URL(link.href, window.location.href).origin !== window.location.origin) {
    link.target = "_blank";
  }
  document.body.appendChild(link);
  link.click();
  link.remove();
}

/** Saves a client-built blob (e.g. a CSV assembled from an already-loaded,
 * already-filtered table) via the browser — the object-URL/anchor-click
 * half of `downloadFile`, split out for a blob that never went through a
 * `fetch` at all (the "current view" for a client-side-filtered table
 * isn't something a server endpoint can reconstruct).
 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** Uploads a file as multipart/form-data (field name "file") and parses
 * a JSON response — `apiRequest` always sends `Content-Type:
 * application/json`, which doesn't fit a file upload.
 */
export async function uploadFile<T>(path: string, file: File): Promise<T> {
  const headers: Record<string, string> = {};
  if (authToken) headers.Authorization = `Bearer ${authToken}`;

  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch(`${API_BASE_URL}${path}`, { method: "POST", headers, body: formData });
  if (response.status === 401) unauthorizedHandler?.();
  await _throwIfNotOk(response);

  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/** Like `uploadFile`, but PUTs a caller-built `FormData` with more than
 * just a bare file field (e.g. branding's logo file alongside
 * primary/accent color text fields in the same multipart body) — kept
 * generic rather than growing `uploadFile` a second, incompatible shape.
 */
export async function putFormData<T>(path: string, formData: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  if (authToken) headers.Authorization = `Bearer ${authToken}`;

  const response = await fetch(`${API_BASE_URL}${path}`, { method: "PUT", headers, body: formData });
  if (response.status === 401) unauthorizedHandler?.();
  await _throwIfNotOk(response);

  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/** Streams a file as the raw request body (`application/octet-stream`,
 * not multipart) with upload progress — `fetch` can't report upload
 * progress, so this uses XMLHttpRequest. For large uploads the backend
 * writes straight to disk (e.g. appliance update bundles, several GB). */
export function uploadRawWithProgress<T>(
  path: string,
  file: Blob,
  onProgress?: (loaded: number, total: number) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE_URL}${path}`);
    if (authToken) xhr.setRequestHeader("Authorization", `Bearer ${authToken}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded, event.total);
    };
    xhr.onerror = () => reject(new ApiError(0, "network_error", "The upload was interrupted (network error)."));
    xhr.onabort = () => reject(new ApiError(0, "aborted", "The upload was cancelled."));
    xhr.onload = () => {
      if (xhr.status === 401) unauthorizedHandler?.();
      let payload: unknown = null;
      try {
        payload = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        payload = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload as T);
        return;
      }
      const body = (payload ?? {}) as { code?: string; message?: string; correlation_id?: string; details?: unknown };
      reject(
        new ApiError(xhr.status, body.code ?? "unknown_error", body.message ?? xhr.statusText, body.correlation_id, body.details),
      );
    };
    xhr.send(file);
  });
}

/** The API root without the version (".../api"): where the backend serves
 * its interactive docs (`/api/docs`) and the OpenAPI schema
 * (`/api/openapi.json`) -- under /api/ so the appliance's reverse proxy
 * forwards them like every other API call. */
export const API_ROOT_URL = API_BASE_URL.replace(/\/v\d+\/?$/, "");
export const API_DOCS_URL = `${API_ROOT_URL}/docs`;
export const OPENAPI_JSON_URL = `${API_ROOT_URL}/openapi.json`;

/** Saves the live OpenAPI schema as a file, named after the running
 * version (`info.version`) so downloads from different releases don't
 * overwrite each other. The schema is public, no token needed. */
export async function downloadOpenApiSchema(): Promise<void> {
  const response = await fetch(OPENAPI_JSON_URL);
  await _throwIfNotOk(response);
  const text = await response.text();
  let version = "";
  try {
    version = String((JSON.parse(text) as { info?: { version?: string } }).info?.version ?? "");
  } catch {
    // Not JSON after all: still hand over what the server sent.
  }
  const safeVersion = version.replace(/[^\w.-]/g, "");
  downloadBlob(
    new Blob([text], { type: "application/json" }),
    safeVersion ? `resurgam-openapi-${safeVersion}.json` : "resurgam-openapi.json",
  );
}
