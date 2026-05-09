const CACHE = "drunk-v1";

// Fichiers à mettre en cache lors de l'installation
const PRECACHE = [
  "/",
  "/index.html",
  "/blackjack.html",
  "/manifest.json",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

// ── Installation ──────────────────────────────────────────────────────────────
self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE))
  );
  self.skipWaiting();
});

// ── Activation (supprime les vieux caches) ────────────────────────────────────
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ── Fetch : network-first pour les données, cache-first pour les assets ───────
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // WebSocket : pas de cache
  if (e.request.url.startsWith("ws")) return;

  // API Render : toujours réseau (données live)
  if (url.hostname.includes("onrender.com")) return;

  // Pages HTML et assets : network-first avec fallback cache
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        // Met à jour le cache si succès
        if (res && res.status === 200 && e.request.method === "GET") {
          const clone = res.clone();
          caches.open(CACHE).then((cache) => cache.put(e.request, clone));
        }
        return res;
      })
      .catch(() => {
        // Offline : sert depuis le cache
        return caches.match(e.request).then((cached) => {
          if (cached) return cached;
          // Page offline si rien dans le cache
          return caches.match("/index.html");
        });
      })
  );
});
