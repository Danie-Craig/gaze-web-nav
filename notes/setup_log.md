# WebArena Setup Log

## Machine
- **Machine:** liralab-widowx-Alienware-Aurora-R16
- **OS:** Ubuntu 22.04.5 LTS
- **Working directory:** /data/webarena-setup/
- **Docker:** Root access available

## Setup Date
May 19, 2026

## Fixes Applied (not in official docs)

### 1. GitLab Port Mapping
The gitlab image is pre-configured for port 8023 internally.
The official setup script maps 9001:9001 which does not work.
**Fix:** Map 9001:8023 instead.
```bash
docker create --name gitlab -p 9001:8023 gitlab-populated-final-port8023 /opt/gitlab/embedded/bin/runsvdir-start
```

### 2. Map Tile URL
The leaflet.osm.js file pointed to CMU tile server (inaccessible).
**Fix:**
```bash
sed -i "s|http://ogma.lti.cs.cmu.edu:8080/tile/{z}/{x}/{y}.png|https://tile.openstreetmap.org/{z}/{x}/{y}.png|g" /data/webarena-setup/webarena/openstreetmap-website/vendor/assets/leaflet/leaflet.osm.js
```

### 3. Nominatim (Map Search) URL
settings.yml pointed to CMU Nominatim server (inaccessible).
**Fix:**
```bash
sed -i "s|nominatim_url:.*|nominatim_url: \"https://nominatim.openstreetmap.org/\"|g" /data/webarena-setup/webarena/openstreetmap-website/config/settings.yml
```

### 4. OpenStreetMap Database Migration
Map showed PendingMigrationError on first load.
**Fix:**
```bash
docker exec openstreetmap-website-web-1 bin/rails db:migrate RAILS_ENV=development
```

### 5. Homepage Port
Homepage Flask app runs on port 4399 by default.
The 06_serve_homepage.sh script tries port 80 (needs root).
**Fix:** Run directly without sudo:
```bash
cd /data/webarena-setup/webarena/webarena-homepage
nohup /data/webarena-setup/webarena/venv/bin/python app.py &
```

### 6. Homepage Map URL
index.html pointed to localhost:443 for Map.
**Fix:**
```bash
sed -i "s|http://localhost:443|http://localhost:3000|g" /data/webarena-setup/webarena/webarena-homepage/templates/index.html
```

### 7. Shopping/Map URL Patch
The 03_docker_create_containers.sh script fails early due to set -e.
Run the patch script manually after starting containers:
```bash
source 00_vars.sh
bash 05_docker_patch_containers.sh
```

## Auth Files
Generated using auto_login.py. Stored at:
/data/webarena-setup/webarena-tasks/.auth/

## Environment Variables (saved to ~/.bashrc)
```bash
export REDDIT="http://localhost:8080"
export SHOPPING="http://localhost:8082"
export SHOPPING_ADMIN="http://localhost:8083/admin"
export GITLAB="http://localhost:9001"
export WIKIPEDIA="http://localhost:8081"
export MAP="http://localhost:3000"
export HOMEPAGE="http://localhost:4399"
```

## Credentials
| Site | Username | Password |
|------|----------|----------|
| Shopping | emma.lopez@gmail.com | Password.123 |
| Shopping Admin | admin | admin1234 |
| GitLab | byteblaze | hello1234 |
| Reddit | MarvelsGrantMan136 | test1234 |
