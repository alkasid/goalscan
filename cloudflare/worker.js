/**
 * GoalScan proxy API-Football — Cloudflare Worker (ES module)
 *
 * Fix lag dashboard (2026-09-06):
 *  - CACHE EDGE 15s per chunk di ids: N tab aperte = 1 sola chiamata upstream
 *    ogni 15s invece di N chiamate ogni 10s.
 *  - STALE-ON-ERROR: se API-Football risponde con rateLimit/errore/eccezione,
 *    serve l'ultima risposta buona (fino a 10 min) invece di results:0.
 *  - Chiave ids normalizzata (ordinata) per non frammentare la cache.
 *
 * Deploy: sostituisce integralmente il worker esistente su
 * spring-hall-b29e.nwgir.workers.dev. Richiede il secret API_FOOTBALL_KEY:
 *   wrangler secret put API_FOOTBALL_KEY   (oppure Dashboard → Settings → Variables)
 */

const FRESH_TTL = 15;        // s — finestra di condivisione tra i client
const STALE_TTL = 600;       // s — quanto a lungo vale l'ultima risposta buona
const ALLOWED_ENDPOINTS = new Set(["fixtures"]);
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS, ...extra },
  });
}

// errors di API-Football: [] quando ok, {} o {chiave: msg} quando errore
function hasApiErrors(data) {
  const e = data && data.errors;
  if (!e) return false;
  if (Array.isArray(e)) return e.length > 0;
  return Object.keys(e).length > 0;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (request.method !== "GET") return json({ errors: { method: "GET only" } }, 405);

    const url = new URL(request.url);
    const endpoint = url.searchParams.get("endpoint") || "";
    const idsRaw = url.searchParams.get("ids") || "";

    if (!ALLOWED_ENDPOINTS.has(endpoint))
      return json({ errors: { endpoint: "endpoint non consentito" } }, 400);
    if (!/^\d+(-\d+)*$/.test(idsRaw))
      return json({ errors: { ids: "formato ids non valido" } }, 400);

    const ids = [...new Set(idsRaw.split("-"))].sort();
    if (ids.length > 20)
      return json({ errors: { ids: "max 20 ids per richiesta" } }, 400);
    const idsKey = ids.join("-");

    const cache = caches.default;
    // chiavi sintetiche: stessa lista di ids (in qualunque ordine) → stessa entry
    const freshKey = new Request(`https://cache.goalscan/fresh/${endpoint}/${idsKey}`);
    const staleKey = new Request(`https://cache.goalscan/stale/${endpoint}/${idsKey}`);

    // 1) cache fresca (≤15s): risposta condivisa tra tutte le tab
    const freshHit = await cache.match(freshKey);
    if (freshHit) {
      const body = await freshHit.text();
      return new Response(body, {
        headers: { "Content-Type": "application/json", "X-Cache": "HIT", ...CORS },
      });
    }

    // 2) upstream API-Football
    let data = null;
    let upstreamOk = false;
    try {
      const r = await fetch(
        `https://v3.football.api-sports.io/fixtures?ids=${idsKey}`,
        { headers: { "x-apisports-key": env.API_FOOTBALL_KEY } }
      );
      data = await r.json();
      upstreamOk = r.ok && !hasApiErrors(data);
    } catch (_) {
      upstreamOk = false;
    }

    if (upstreamOk) {
      const body = JSON.stringify(data);
      // scrive sia la copia fresca (15s) sia la last-good (10 min)
      ctx.waitUntil(cache.put(freshKey, new Response(body, {
        headers: { "Content-Type": "application/json", "Cache-Control": `max-age=${FRESH_TTL}` },
      })));
      ctx.waitUntil(cache.put(staleKey, new Response(body, {
        headers: { "Content-Type": "application/json", "Cache-Control": `max-age=${STALE_TTL}` },
      })));
      return new Response(body, {
        headers: { "Content-Type": "application/json", "X-Cache": "MISS", ...CORS },
      });
    }

    // 3) upstream fallito (rateLimit ecc.) → ultima risposta buona se esiste
    const staleHit = await cache.match(staleKey);
    if (staleHit) {
      const body = await staleHit.text();
      return new Response(body, {
        headers: { "Content-Type": "application/json", "X-Cache": "STALE", ...CORS },
      });
    }

    // 4) niente in cache: inoltra l'errore reale (il frontend lo gestirà)
    return json(data || { errors: { upstream: "fetch fallita" } }, 200, { "X-Cache": "NONE" });
  },
};
