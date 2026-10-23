// Butler-service worker (zoals in CellarPeaks): houdt de app-schil offline beschikbaar, toont een nette
// "wordt bijgewerkt"-pagina wanneer Tailscale tijdens een herstart 502–504 geeft, en toont Web Push-meldingen.
// De API wordt nooit gecachet: die gaat altijd naar de server.
const CACHE = "butler-v11"; // een nieuwe versie wist de oudere caches
const SHELL = ["/", "/manifest.webmanifest", "/icon.svg", "/apple-touch-icon.png", "/icon-192.png", "/badge-96.png", "/fonts/bodoni-moda-latin.woff2"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

const WAIT = `<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="3">
<title>Butler wordt bijgewerkt</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; font: 17px/1.5 -apple-system, system-ui, sans-serif;
         background: #ffffff; color: #000000; text-align: center; padding: 24px; box-sizing: border-box; }
  @media (prefers-color-scheme: dark) { body { background: #000000; color: #ffffff; } }
  img { width: 72px; height: 72px; border-radius: 16px; } p { margin: 4px 0; } small { opacity: .6; }
</style></head><body><div><img src="/apple-touch-icon.png" alt=""><p><b>Butler wordt bijgewerkt</b></p>
<p>Een ogenblik: deze pagina herlaadt vanzelf.</p><small>Meestal duurt dit minder dan een minuut.</small></div></body></html>`;
const restarting = (response) => response.status >= 502 && response.status <= 504;
const wait = () => new Response(WAIT, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request, { cache: "no-cache" }).then((response) => {
      if (restarting(response)) return wait();
      // enkel de app zelf bewaren, niet de aanmeldpagina (een omleiding naar /login)
      if (response.ok && !response.redirected && url.pathname === "/") { const copy = response.clone(); caches.open(CACHE).then((cache) => cache.put("/", copy)); }
      return response;
    }).catch(() => caches.match(url.pathname === "/login" ? "/login" : "/").then((hit) => hit || wait())));
    return;
  }
  event.respondWith(
    fetch(event.request, { cache: "no-cache" })
      .then((response) => {
        if (restarting(response)) return caches.match(event.request).then((hit) => hit || response);
        if (response.ok) { const copy = response.clone(); caches.open(CACHE).then((cache) => cache.put(event.request, copy)); }
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});

// Web Push: {title, body, url, tag}. Een nieuwere melding over dezelfde thread vervangt de oudere.
self.addEventListener("push", (event) => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; } catch { msg = { body: event.data?.text() }; }
  const shown = self.registration.showNotification(msg.title || "Butler", {
    body: msg.body || "", icon: "/icon-192.png", badge: "/badge-96.png",
    data: { url: msg.url || "/" }, tag: msg.tag || "butler",
  });
  // het getal op het app-icoon bijwerken, ook als de app dicht is (iOS 16.4+ op het beginscherm)
  let badge = Promise.resolve();
  if (typeof msg.badge === "number" && self.navigator.setAppBadge) {
    badge = (msg.badge ? self.navigator.setAppBadge(msg.badge) : self.navigator.clearAppBadge()).catch(() => {});
  }
  event.waitUntil(Promise.all([shown, badge]));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const app = open.find((c) => c.url.startsWith(self.location.origin));
    if (app) { // al open: naar voren halen en de juiste mail openen
      await app.focus();
      const hash = new URL(url).hash;
      if (hash) app.postMessage({ open: hash });
      return;
    }
    await self.clients.openWindow(url);
  })());
});
