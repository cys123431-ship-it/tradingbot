// Cloudflare Worker: serves the BTC options board (index.html + tokens.css).
// All market data is fetched by the browser from public Deribit / Binance endpoints,
// so the worker only serves two static files.
import html from "./index.html";
import css from "./tokens.css";

export default {
  async fetch(request) {
    const { pathname } = new URL(request.url);
    if (pathname === "/tokens.css") {
      return new Response(css, { headers: { "content-type": "text/css; charset=utf-8" } });
    }
    if (pathname === "/" || pathname === "/index.html") {
      return new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } });
    }
    return new Response("Not found", { status: 404 });
  },
};
