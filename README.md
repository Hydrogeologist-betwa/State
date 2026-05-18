# GeoReport v2 — Zero Shapefile Edition
# Deploy to the public web in minutes

## What changed from v1
- ❌ No shapefiles to upload
- ❌ No Google Drive
- ✅ Watershed is computed from the DEM (pysheds D8 algorithm)
- ✅ Streams scraped live from OpenStreetMap Overpass API
- ✅ Aquifer fetched live from IGRAC GGMN / WHYMAP
- ✅ Works for any location on Earth

---

## Files
```
app.py           ← Flask backend (all the GIS logic)
index.html       ← Frontend (drop on any web host)
requirements.txt ← Python dependencies
Dockerfile       ← For containerised deployment
```

---

## Option A — Deploy to Railway (FREE, easiest)

1. Push these files to a GitHub repo
2. Go to https://railway.app → New Project → Deploy from GitHub
3. Set environment variable:
   ```
   OPENTOPO_API_KEY = your_key_here
   ```
4. Railway auto-detects the Dockerfile and deploys
5. Copy the public URL (e.g. https://georep.up.railway.app)
6. Paste it into the "Backend URL" field in index.html

Free tier: 500 hrs/month — enough for a public demo.

---

## Option B — Deploy to Render (FREE)

1. Push to GitHub
2. Go to https://render.com → New → Web Service → connect repo
3. Set:
   - Build command : (leave blank, Dockerfile is detected)
   - Start command : gunicorn app:app --bind 0.0.0.0:5000 --timeout 300
4. Add env var: OPENTOPO_API_KEY
5. Deploy → get your public URL

---

## Option C — Deploy to DigitalOcean Droplet ($6/mo)

```bash
# On server:
git clone https://github.com/yourname/georep .
cd georep

# Install deps
apt-get install -y gdal-bin libgdal-dev libgeos-dev libproj-dev build-essential
pip install -r requirements.txt

# Set API key
export OPENTOPO_API_KEY="your_key"

# Run with gunicorn
gunicorn app:app --bind 0.0.0.0:5000 --workers 2 --timeout 300 --daemon
```

Then point your domain's DNS A-record at the server IP and add nginx + SSL.

---

## Hosting the frontend

`index.html` is a plain HTML file. Host it for free on:

- **Netlify** : drag and drop index.html at app.netlify.com
- **Vercel**  : `vercel deploy` in the folder
- **GitHub Pages** : enable Pages on your repo

After hosting, update the default API URL in index.html line:
```html
<input type="text" id="api-url" value="https://YOUR-BACKEND-URL.com" .../>
```

---

## Get your OpenTopography API key (free)

1. Go to https://portal.opentopography.org/requestApiKey
2. Register (free)
3. Copy your key
4. Set it as OPENTOPO_API_KEY env var on your server

---

## Performance notes

- Each report takes 30–90 seconds (all live API calls)
- For high traffic, add Redis caching (cache by lat/lon rounded to 3dp)
- gunicorn --workers 2 handles 2 concurrent requests
- DEM download is the slowest step (~10–20s)

---

## Data attribution (required)

When using publicly:
- OpenTopography / SRTM: NASA JPL
- OpenStreetMap: © OpenStreetMap contributors
- IGRAC / WHYMAP: UN-IGRAC
- HydroSHEDS: WWF / USGS
