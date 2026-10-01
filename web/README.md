# Jarvise Command Dashboard (Vite + React + Tailwind)

Private owner UI served by FastAPI on `:8080`.

```bash
cd web
npm install
npm run dev     # http://127.0.0.1:5173 with API proxy to :8080
npm run build   # output → web/dist (Docker copies into the web image)
```

Routes: `/` Home · `/paper` · `/decisions` · `/exchange` · `/ops`
