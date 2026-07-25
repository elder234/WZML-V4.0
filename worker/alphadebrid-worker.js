/**
 * AlphaDebrid Cloudflare Worker v4
 *
 * Single purpose: decrypt an encrypted RD download URL and 302 redirect.
 * Bot calls RD API directly through WARP — worker only handles the final
 * download link so aria2 gets a clean URL with the filename in the path.
 *
 * Route:
 *   GET /<filename>?f=<encrypted_direct_url>  — decrypt + 302 redirect
 *
 * Secrets (CF dashboard > Worker > Settings > Variables and Secrets):
 *   API_KEY  - 32-char AES key shared with bot (same as CF_WORKER_KEY in config)
 */

export default {
  async fetch(request, env) {
    if (!env.API_KEY || env.API_KEY.length !== 32)
      return new Response("Worker misconfigured: API_KEY must be 32 chars", { status: 500 });

    const url = new URL(request.url);
    const encryptedParam = url.searchParams.get("f");
    if (!encryptedParam)
      return new Response("Missing parameter", { status: 400 });

    try {
      const directUrl = await decrypt(encryptedParam, env.API_KEY);
      if (!directUrl.startsWith("http://") && !directUrl.startsWith("https://"))
        return new Response("Decryption failed: invalid URL", { status: 400 });
      return Response.redirect(directUrl, 302);
    } catch (err) {
      return new Response(`Error: ${err.message}`, { status: 500 });
    }
  },
};

async function decrypt(encryptedParam, apiKey) {
  const outerDecoded = atob(decodeURIComponent(encryptedParam));
  const iv = Uint8Array.from(atob(outerDecoded.slice(0, 24)), c => c.charCodeAt(0));
  const ciphertext = Uint8Array.from(atob(outerDecoded.slice(24)), c => c.charCodeAt(0));
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(apiKey), { name: "AES-CBC" }, false, ["decrypt"]);
  const decrypted = await crypto.subtle.decrypt({ name: "AES-CBC", iv }, key, ciphertext);
  return new TextDecoder().decode(decrypted);
}
