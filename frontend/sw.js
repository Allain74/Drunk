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

// ── IndexedDB helpers (stockage persistant des notifs hors-page) ──────────────
const IDB_NAME    = "drunk-notifs";
const IDB_VERSION = 1;
const IDB_STORE   = "pending";

function _idbOpen() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_NAME, IDB_VERSION);
    req.onupgradeneeded = (e) => {
      e.target.result.createObjectStore(IDB_STORE, { keyPath: "id" });
    };
    req.onsuccess = (e) => resolve(e.target.result);
    req.onerror   = (e) => reject(e.target.error);
  });
}

function _idbSaveNotif(notif) {
  return _idbOpen().then((db) => new Promise((resolve, reject) => {
    const tx = db.transaction(IDB_STORE, "readwrite");
    tx.objectStore(IDB_STORE).put(notif);
    tx.oncomplete = () => resolve();
    tx.onerror    = (e) => reject(e.target.error);
  }));
}

// ── Push notifications ────────────────────────────────────────────────────────
self.addEventListener("push", (event) => {
  if (!event.data) return;
  let data = {};
  try { data = event.data.json(); } catch(e) { data = { title: "Drunk 🍺", body: event.data.text() }; }

  const notif = {
    id: Date.now() + Math.random(),
    title: data.title || "Drunk 🍺",
    body: data.body || "",
    url: data.url || "/",
    at: Date.now(),
    read: false,
  };

  event.waitUntil(
    Promise.all([
      // 1. Afficher la notification système
      self.registration.showNotification(notif.title, {
        body: notif.body,
        icon:  "/icons/icon-192.png",
        badge: "/icons/icon-192.png",
        data:  { url: notif.url },
      }),

      // 2. Stocker dans IndexedDB (persiste même si aucune page ouverte)
      _idbSaveNotif(notif).catch(() => {}),

      // 3. Transmet la notif aux pages ouvertes pour stockage localStorage immédiat
      self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(clients => {
        clients.forEach(c => c.postMessage({ type: "PUSH_NOTIF", notif }));
      }),
    ])
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if (client.url.includes(target) && "focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(target);
    })
  );
});
