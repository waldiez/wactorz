#!/bin/bash
#   Licensed to the Apache Software Foundation (ASF) under one or more
#   contributor license agreements.  See the NOTICE file distributed with
#   this work for additional information regarding copyright ownership.
#   The ASF licenses this file to You under the Apache License, Version 2.0
#   (the "License"); you may not use this file except in compliance with
#   the License.  You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

# cspell: disable
# shellcheck disable=SC2016,SC2086,SC2091

set -e

mkdir -p $FUSEKI_BASE/databases

if [ ! -f "$FUSEKI_BASE/shiro.ini" ] ; then
  # First time
  echo "###################################"
  echo "Initializing Apache Jena Fuseki"
  echo ""
  cp "$FUSEKI_HOME/shiro.ini" "$FUSEKI_BASE/shiro.ini"
  if [ -z "$ADMIN_PASSWORD" ] ; then
    ADMIN_PASSWORD=$(pwgen -s 15)
    echo "Randomly generated admin password:"
    echo ""
    echo "admin=$ADMIN_PASSWORD"
  fi
  echo ""
  echo "###################################"
fi

if [ -d "/fuseki-extra" ] && [ ! -d "$FUSEKI_BASE/extra" ] ; then
  ln -s "/fuseki-extra" "$FUSEKI_BASE/extra" 
fi

# $ADMIN_PASSWORD only modifies if ${ADMIN_PASSWORD}
# is in shiro.ini
if [ -n "$ADMIN_PASSWORD" ] ; then
  export ADMIN_PASSWORD
  envsubst '${ADMIN_PASSWORD}' < "$FUSEKI_BASE/shiro.ini" > "$FUSEKI_BASE/shiro.ini.$$" && \
    mv "$FUSEKI_BASE/shiro.ini.$$" "$FUSEKI_BASE/shiro.ini"
    cp "$FUSEKI_BASE/shiro.ini" "$FUSEKI_HOME/shiro.ini"
  export ADMIN_PASSWORD
fi
if [ -n "$ADMIN_USER" ] ; then
  export ADMIN_USER
  envsubst '${ADMIN_USER}' < "$FUSEKI_BASE/shiro.ini" > "$FUSEKI_BASE/shiro.ini.$$" && \
    mv "$FUSEKI_BASE/shiro.ini.$$" "$FUSEKI_BASE/shiro.ini"
    cp "$FUSEKI_BASE/shiro.ini" "$FUSEKI_HOME/shiro.ini"
  export ADMIN_USER
fi

# fork 
exec "$@" &

# TDB_VERSION=''
# if [ ! -z ${TDB+x} ] && [ "${TDB}" = "2" ] ; then 
#   TDB_VERSION='tdb2'
# else
#   TDB_VERSION='tdb'
# fi

# Wait until server is up
echo "Waiting for Fuseki to finish starting up..."
until $(curl --output /dev/null --silent --head --fail http://localhost:3030); do
  sleep 1s
done
# -----------------------------------------------------------------------------
# Create datasets from env: FUSEKI_DATASET_* = datasetName
# -----------------------------------------------------------------------------
# if [ "${FUSEKI_CREATE_DATASETS}" = "true" ]; then
#   DATASET_VARS="$(printenv | grep -E '^FUSEKI_DATASET_' || true)"

#   if [ -n "${DATASET_VARS}" ]; then
#     echo "Creating datasets from FUSEKI_DATASET_* env vars..."

#     # Build curl auth args safely (as separate args)
#     AUTH_1=""
#     AUTH_2=""
#     if [ -n "${ADMIN_PASSWORD}" ]; then
#       AUTH_1="-u"
#       AUTH_2="admin:${ADMIN_PASSWORD}"
#     fi

#     echo "${DATASET_VARS}" | while IFS= read -r line; do
#       dataset="${line#*=}"
#       [ -n "${dataset}" ] || continue

#       echo "Creating dataset: ${dataset} (${TDB_VERSION})"

#       # Best-effort create: if it already exists, Fuseki returns 409; ignore.
#       if [ -n "${AUTH_1}" ]; then
#         curl -sS "${AUTH_1}" "${AUTH_2}" \
#           -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \
#           --data "dbName=${dataset}&dbType=${TDB_VERSION}" \
#           "http://localhost:${FUSEKI_PORT}/\$/datasets" \
#           >/dev/null 2>&1 || true
#       else
#         curl -sS \
#           -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \
#           --data "dbName=${dataset}&dbType=${TDB_VERSION}" \
#           "http://localhost:${FUSEKI_PORT}/\$/datasets" \
#           >/dev/null 2>&1 || true
#       fi
#     done
#   fi
# fi
echo "Fuseki is available :-)"
unset ADMIN_PASSWORD # Don't keep it in memory
unset ADMIN_USER # Don't keep it in memory

# rejoin our exec
wait
