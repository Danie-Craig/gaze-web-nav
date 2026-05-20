#!/bin/bash
echo "Starting WebArena containers..."
docker start shopping shopping_admin forum gitlab wikipedia

echo "Starting OpenStreetMap..."
cd /data/webarena-setup/webarena/openstreetmap-website
docker compose start

echo "Starting homepage..."
cd /data/webarena-setup/webarena/webarena-homepage
nohup /data/webarena-setup/webarena/venv/bin/python app.py > /tmp/homepage.log 2>&1 &

echo "All done! Sites available at:"
echo "  Shopping:       http://localhost:8082"
echo "  Shopping Admin: http://localhost:8083/admin"
echo "  Forum:          http://localhost:8080"
echo "  GitLab:         http://localhost:9001"
echo "  Wikipedia:      http://localhost:8081"
echo "  Map:            http://localhost:3000"
echo "  Homepage:       http://localhost:4399"
