"""
AturUang Stage 9 — PWA Assets Module.

Provides Web App Manifest, Service Worker script payload with strict NO-CACHE
financial API policy, and SVG application icon.
"""

from __future__ import annotations

import json
from typing import Any

CACHE_VERSION = "v1"
CACHE_NAME = f"aturuang-shell-{CACHE_VERSION}"

PWA_MANIFEST: dict[str, Any] = {
    "name": "AturUang",
    "short_name": "AturUang",
    "description": "Pengelola Keuangan Pribadi & Bisnis Mandiri",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait-primary",
    "theme_color": "#1e293b",
    "background_color": "#0f172a",
    "icons": [
        {
            "src": "/icon.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any maskable",
        },
        {
            "src": "/icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any maskable",
        },
        {
            "src": "/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable",
        },
    ],
}

SERVICE_WORKER_JS: str = f"""// AturUang Service Worker — Shell Cache & Privacy Preserving Router
// Version: {CACHE_VERSION}
const CACHE_NAME = '{CACHE_NAME}';
const SHELL_ASSETS = [
  '/',
  '/manifest.webmanifest',
  '/icon.svg'
];

// Install: Cache static shell assets
self.addEventListener('install', (event) => {{
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {{
      return cache.addAll(SHELL_ASSETS);
    }}).then(() => {{
      return self.skipWaiting();
    }})
  );
}});

// Activate: Clean up old shell caches
self.addEventListener('activate', (event) => {{
  event.waitUntil(
    caches.keys().then((keys) => {{
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    }}).then(() => {{
      return self.clients.claim();
    }})
  );
}});

// Fetch: Strict privacy-preserving caching policy
self.addEventListener('fetch', (event) => {{
  const url = new URL(event.request.url);

  // STRICT PRIVACY & SECURITY: Financial API endpoints MUST NEVER be cached.
  // All requests to /api/* bypass CacheStorage completely and use network-only.
  if (url.pathname.startsWith('/api/')) {{
    event.respondWith(fetch(event.request));
    return;
  }}

  // Non-GET requests should always go to network
  if (event.request.method !== 'GET') {{
    event.respondWith(fetch(event.request));
    return;
  }}

  // App shell caching: cache-first with background revalidation for static shell assets
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {{
      if (cachedResponse) {{
        fetch(event.request).then((networkResponse) => {{
          if (networkResponse && networkResponse.status === 200) {{
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, networkResponse));
          }}
        }}).catch(() => {{}});
        return cachedResponse;
      }}

      return fetch(event.request).then((networkResponse) => {{
        if (!networkResponse || networkResponse.status !== 200 || networkResponse.type !== 'basic') {{
          return networkResponse;
        }}
        const responseToCache = networkResponse.clone();
        caches.open(CACHE_NAME).then((cache) => {{
          cache.put(event.request, responseToCache);
        }});
        return networkResponse;
      }}).catch(() => {{
        if (event.request.mode === 'navigate') {{
          return caches.match('/');
        }}
      }});
    }})
  );
}});
"""

ICON_SVG: str = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="128" fill="#1e293b"/>
  <path d="M128 160h256a32 32 0 0 1 32 32v128a32 32 0 0 1-32 32H128a32 32 0 0 1-32-32V192a32 32 0 0 1 32-32z" fill="#0f172a" stroke="#10b981" stroke-width="24"/>
  <circle cx="336" cy="256" r="24" fill="#10b981"/>
</svg>"""


def get_manifest_json() -> str:
    """Returns serialized JSON manifest for Progressive Web App."""
    return json.dumps(PWA_MANIFEST, indent=2)


def get_service_worker_js() -> str:
    """Returns Service Worker JavaScript payload."""
    return SERVICE_WORKER_JS


def get_icon_svg() -> str:
    """Returns SVG icon payload."""
    return ICON_SVG
