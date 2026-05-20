# Gaze-Regularized VLM for Web Navigation

**Researcher:** Danie Craig Kulandai  
**Lab:** LIRA Lab, USC  
**Supervisor:** Prof. Erdem Biyik  
**PhD Mentor:** Yutai Zhou  

## Project Overview
Fine-tuning UI-TARS-1.5-7B with human gaze regularization for web navigation on WebArena.

**Core equation:**
L_total = L_action + λ * KL(human_gaze || model_attention)

## Repository Structure
- `setup/` — Scripts to set up and start WebArena
- `data_collection/` — Recording pipeline scripts
- `notes/` — Setup logs and design decisions

## WebArena Sites
| Site | URL |
|------|-----|
| Shopping | http://localhost:8082 |
| Shopping Admin | http://localhost:8083/admin |
| Forum | http://localhost:8080 |
| GitLab | http://localhost:9001 |
| Wikipedia | http://localhost:8081 |
| Map | http://localhost:3000 |
| Homepage | http://localhost:4399 |

## Starting WebArena
```bash
bash /data/webarena-setup/start_webarena.sh
```

## Task Distribution (812 total)
| Site | Tasks |
|------|-------|
| GitLab | 204 |
| Shopping | 192 |
| Shopping Admin | 184 |
| Reddit | 129 |
| Map | 128 |
| Wikipedia | 23 |
