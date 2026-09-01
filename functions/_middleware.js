// Cloudflare Pages middleware — HTTP Basic Auth gate for the whole site.
//
// Set SITE_PASSWORD in the Pages project:
//   Cloudflare dashboard → Workers & Pages → <project> → Settings →
//   Environment variables → add "SITE_PASSWORD" (Production + Preview).
//
// Any username is accepted; only the password is checked. Share it as
// "anything : <password>" or just tell people the password (the browser
// prompts for both fields; the username can be left blank in most browsers,
// otherwise type any word).

export const onRequest = async (context) => {
  const { request, env, next } = context;

  // No password configured yet → don't lock anyone out.
  if (!env.SITE_PASSWORD) return next();

  const header = request.headers.get("Authorization") || "";
  const [scheme, encoded] = header.split(" ");

  if (scheme === "Basic" && encoded) {
    let decoded = "";
    try { decoded = atob(encoded); } catch (_) { /* malformed */ }
    const pass = decoded.slice(decoded.indexOf(":") + 1);
    if (timingSafeEqual(pass, env.SITE_PASSWORD)) return next();
  }

  return new Response("Authentication required.", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="NFL Dashboard", charset="UTF-8"',
      "Cache-Control": "no-store",
    },
  });
};

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) {
    return false;
  }
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}
