# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
################################################################################

FROM gcr.io/oss-fuzz-base/base-builder
RUN apt-get update && apt install -y automake autoconf libtool pkg-config python3-docutils python3-sphinx libedit-dev libpcre2-dev libncurses-dev
# Robust git clone with retry logic and fallback protocols
RUN for protocol in https git; do \
    for attempt in 1 2 3; do \
        echo "Attempt $attempt: Cloning varnish-cache using $protocol protocol..." && \
        if [ "$protocol" = "https" ]; then \
            git clone --depth 1 --recursive https://github.com/varnishcache/varnish-cache && break; \
        else \
            git clone --depth 1 --recursive git://github.com/varnishcache/varnish-cache && break; \
        fi || \
        echo "Clone attempt $attempt with $protocol protocol failed, retrying in 5 seconds..." && \
        sleep 5; \
    done && break; \
done || { \
    echo "All clone attempts failed. Trying shallow clone without submodules..." && \
    git clone --depth 1 https://github.com/varnishcache/varnish-cache && \
    cd varnish-cache && \
    git submodule init && \
    for i in 1 2 3; do \
        git submodule update --depth 1 && break || \
        echo "Submodule update attempt $i failed, retrying in 10 seconds..." && \
        sleep 10; \
    done; \
    if [ $? -ne 0 ]; then \
        echo "Warning: Submodule update failed, but main repository cloned successfully."; \
    fi; \
}
COPY run_tests.sh build.sh $SRC
WORKDIR $SRC/varnish-cache
