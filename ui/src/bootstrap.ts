/** Read the launcher token without changing the URL needed by fresh documents. */
export function readToken(url: URL): string {
  return url.searchParams.get("t") ?? "";
}
