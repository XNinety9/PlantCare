// Service worker minimal : l'appli s'installe sur l'écran d'accueil, les fichiers statiques sont
// mis en cache, et une page de secours s'affiche hors connexion. Les données restent toujours en ligne.
const CACHE = "plantcare-v1";
const ASSETS = ["/static/style.css", "/static/icons/icon.svg", "/static/icons/icon-192.png"];

const OFFLINE_PAGE = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PlantCare</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body><main><div class="empty" style="margin-top:20vh">
<p style="font-size:2rem">🌿</p><p>Offline · Hors connexion</p></div></main></body></html>`;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== location.origin) return;

  if (url.pathname.startsWith("/static/")) {
    // Le cache répond tout de suite, et se met à jour en arrière-plan
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const cached = await cache.match(request);
        const network = fetch(request)
          .then((response) => {
            if (response.ok) cache.put(request, response.clone());
            return response;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => new Response(OFFLINE_PAGE, { headers: { "Content-Type": "text/html; charset=utf-8" } }))
    );
  }
});
