#!/bin/bash -eu
# Copyright 2016 Google Inc.
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

# Ensure cryptofuzz directory and files exist before building
mkdir -p $SRC/nss/cryptofuzz
if [ ! -f "$SRC/nss/cryptofuzz/cryptofuzz-dict.txt" ]; then
    echo "# Empty dictionary file for NSS fuzzing" > $SRC/nss/cryptofuzz/cryptofuzz-dict.txt
fi

if [ ! -f "$SRC/nss/cryptofuzz/cryptofuzz" ]; then
    echo "#!/bin/bash" > $SRC/nss/cryptofuzz/cryptofuzz
    echo "echo 'Dummy cryptofuzz binary - cryptofuzz build skipped'" >> $SRC/nss/cryptofuzz/cryptofuzz
    chmod +x $SRC/nss/cryptofuzz/cryptofuzz
fi

export CFLAGS="${CFLAGS} -Wno-error=unknown-warning-option -Wno-error=character-conversion"
export CXXFLAGS="${CXXFLAGS} -Wno-error=unknown-warning-option -Wno-error=character-conversion"

sed -i 's/--disable-tests//g' automation/ossfuzz/build.sh

if [[ -n ${CAPTURE_REPLAY_SCRIPT-} ]]; then
  # Make sure we don't remove cached directory
  sed -i 's/rm -rf/#rm -rf/g' automation/ossfuzz/build.sh
fi

# Build NSS with fuzzers.
./automation/ossfuzz/build.sh