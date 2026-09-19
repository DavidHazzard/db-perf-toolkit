#!/usr/bin/env bash
#
# Remove only what this project created.
#
#   ./scripts/docker-clean.sh            containers + their anonymous volumes
#   ./scripts/docker-clean.sh --images   also drop the engine images (~2GB)
#
# Deliberately never runs `docker system prune` or `docker volume prune`.
# Those are global: they reap every dangling volume on the machine, including
# other projects' work. `docker rm -v` removes the anonymous volumes attached
# to the containers being removed, which is the scoped equivalent.

set -euo pipefail

IMAGES=${1:-}

targets=()
# Containers we name ourselves (demo.sh, bench scripts).
while IFS= read -r id; do [[ -n "$id" ]] && targets+=("$id"); done \
  < <(docker ps -aq --filter "name=dbperf-" 2>/dev/null)
# Containers testcontainers started. Ryuk normally reaps these on exit; this
# catches the ones left behind when a test run is killed rather than finished.
while IFS= read -r id; do [[ -n "$id" ]] && targets+=("$id"); done \
  < <(docker ps -aq --filter "label=org.testcontainers=true" 2>/dev/null)

if [[ ${#targets[@]} -eq 0 ]]; then
  echo "No db-perf-toolkit containers to remove."
else
  echo "Removing ${#targets[@]} container(s) and their anonymous volumes:"
  docker inspect --format '  {{.Name}} ({{.Config.Image}})' "${targets[@]}" 2>/dev/null || true
  docker rm -fv "${targets[@]}" >/dev/null
fi

if [[ "$IMAGES" == "--images" ]]; then
  echo
  echo "Removing engine images:"
  for img in postgres:16 mcr.microsoft.com/mssql/server:2022-latest; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      echo "  $img"
      docker rmi "$img" >/dev/null 2>&1 || echo "    (in use, skipped)"
    fi
  done
fi

echo
echo "Project footprint now:"
printf "  containers: %s\n" "$(docker ps -aq --filter 'name=dbperf-' --filter 'label=org.testcontainers=true' 2>/dev/null | wc -l)"
docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' 2>/dev/null \
  | grep -iE "postgres:16|mssql" || echo "  no engine images cached"
