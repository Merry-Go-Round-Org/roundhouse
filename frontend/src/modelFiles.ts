export type ModelFile = { path: string; sha256: string };
export function sourcePath(value: string): string | null {
  let path = value.split("#", 1)[0];
  if (path.startsWith("/inputs/model/")) path = path.slice("/inputs/model/".length);
  if (path.startsWith("./")) path = path.slice(2);
  if (!path || /[\\:%?\x00-\x1f\x7f]/.test(path) || path.startsWith("/") ||
      path.split("/").some(part => !part || part === "." || part === "..") ||
      !/\.(md|txt|json|py|R)$/.test(path)) return null;
  return path;
}
export function readModelFiles(value: unknown): ModelFile[] {
  if (!Array.isArray(value)) throw new Error("Invalid model file list.");
  const paths = new Set<string>();
  return value.map(row => {
    if (!row || typeof row !== "object" || typeof row.path !== "string" ||
        sourcePath(row.path) !== row.path || paths.has(row.path) ||
        typeof row.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(row.sha256))
      throw new Error("Invalid or duplicate model file evidence.");
    paths.add(row.path);
    // Do not follow server-provided download_url; construct a model-bound API path.
    return {path: row.path, sha256: row.sha256};
  });
}
export function modelFileRoute(modelId: string, path: string): string {
  if (sourcePath(path) !== path) throw new Error("Unsupported source path.");
  return `/models/${encodeURIComponent(modelId)}/files/${path.split("/").map(encodeURIComponent).join("/")}`;
}
