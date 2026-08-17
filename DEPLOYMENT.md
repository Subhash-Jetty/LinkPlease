# Deployment

GitHub stores the code and runs CI, but it does not directly host this FastAPI backend through GitHub Pages. GitHub Pages only serves static files.

Use Render with this repo:

1. Open Render and choose **New +** > **Blueprint**.
2. Connect `https://github.com/Subhash-Jetty/LinkPlease`.
3. Render should detect `render.yaml`.
4. Set the secret environment variable:

```text
PSEUDOGRAM_API_KEY=<your PseudoGram key>
```

5. Deploy.
6. After deploy, open:

```text
https://YOUR-RENDER-APP.onrender.com/stats
```

Expected fresh response:

```json
{
  "sent": 0,
  "failed": 0,
  "queued": 0,
  "duplicates_blocked": 0
}
```

Render will mount persistent SQLite storage at `/var/data/linkplease.sqlite3` because `render.yaml` includes a persistent disk.

