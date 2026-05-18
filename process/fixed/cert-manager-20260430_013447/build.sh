cp $SRC/pki_fuzzer.go $SRC/cert-manager/pkg/util/pki/

# First, trigger the Go toolchain download by running go mod tidy
cd $SRC/cert-manager
go mod tidy

# Now the Go 1.25.7 toolchain should be in GOMODCACHE
# Get the GOROOT from the project directory (not from /tmp/go-118-fuzz-build which uses go 1.25.0)
TOOLCHAIN_DIR=$(cd $SRC/cert-manager && go env GOROOT)
echo "Toolchain GOROOT: $TOOLCHAIN_DIR"

# Copy it to a temp directory outside GOMODCACHE so overlay works
cd /tmp/go-118-fuzz-build
cp -r "$TOOLCHAIN_DIR" /tmp/custom_goroot
echo "Copied toolchain to /tmp/custom_goroot"

# Modify go-118-fuzz-build to use the custom GOROOT
python3 << 'PYEOF'
with open('file_walker.go', 'r') as f:
    content = f.read()

# Replace getGoRootPath to return custom path
old_func = '''func getGoRootPath() string {
\tout, err := exec.Command("go", "env", "-json").Output()
\tif err != nil {
\t\tpanic(err)
\t}
\tm := make(map[string]string)
\terr = json.Unmarshal(out, &m)
\tif err != nil {
\t\tpanic(err)
\t}
\tgoRootDir := m["GOROOT"]
\treturn goRootDir
}'''

new_func = '''func getGoRootPath() string {
\treturn "/tmp/custom_goroot"
}'''

if old_func in content:
    content = content.replace(old_func, new_func)
    print("Patched getGoRootPath")
else:
    print("ERROR: Could not find getGoRootPath function")
    exit(1)

# Remove unused import of "os/exec" if it's no longer needed
if '"os/exec"' in content and 'exec.Command' not in content and 'exec.LookPath' not in content:
    content = content.replace('\t"os/exec"\n', '')
    print("Removed unused os/exec import")

with open('file_walker.go', 'w') as f:
    f.write(content)
PYEOF

# Also patch the go build command in main.go to use the custom go binary
python3 << 'PYEOF'
with open('main.go', 'r') as f:
    content = f.read()

# Find the exec.Command("go", args...) line and add env
old_cmd = 'cmd := exec.Command("go", args...)'
new_cmd = '''cmd := exec.Command("/tmp/custom_goroot/bin/go", args...)
\tcmd.Env = append(os.Environ(), "GOROOT=/tmp/custom_goroot")'''

if old_cmd in content:
    content = content.replace(old_cmd, new_cmd)
    print("Patched go command")
else:
    print("ERROR: Could not find go command")
    exit(1)

with open('main.go', 'w') as f:
    f.write(content)
PYEOF

go build -o /root/go/bin/go-118-fuzz-build_v2 .

cd $SRC/cert-manager
compile_native_go_fuzzer_v2 github.com/cert-manager/cert-manager/internal/webhook/admission/certificaterequest/approval FuzzValidate FuzzValidate_approval
compile_native_go_fuzzer_v2 github.com/cert-manager/cert-manager/pkg/controller/certificates/trigger FuzzProcessItem FuzzProcessItem_trigger
compile_native_go_fuzzer_v2 github.com/cert-manager/cert-manager/pkg/controller/certificates/revisionmanager FuzzProcessItem FuzzProcessItem_revisionmanager
compile_native_go_fuzzer_v2 github.com/cert-manager/cert-manager/pkg/controller/certificates/issuing FuzzProcessItem FuzzProcessItem_issuing
compile_native_go_fuzzer_v2 github.com/cert-manager/cert-manager/pkg/controller/certificates/readiness FuzzProcessItem FuzzProcessItem_readiness
compile_native_go_fuzzer_v2 github.com/cert-manager/cert-manager/pkg/controller/certificates/keymanager FuzzProcessItem FuzzProcessItem_keymanager
compile_native_go_fuzzer_v2 github.com/cert-manager/cert-manager/pkg/controller/certificates/requestmanager FuzzProcessItem FuzzProcessItem_requestmanager
compile_native_go_fuzzer_v2 github.com/cert-manager/cert-manager/pkg/controller/certificaterequests/vault FuzzVaultCRController FuzzVaultCRController
compile_native_go_fuzzer_v2 github.com/cert-manager/cert-manager/pkg/controller/certificaterequests/venafi FuzzVenafiCRController FuzzVenafiCRController
compile_go_fuzzer github.com/cert-manager/cert-manager/pkg/util/pki FuzzUnmarshalSubjectStringToRDNSequence FuzzUnmarshalSubjectStringToRDNSequence
compile_go_fuzzer github.com/cert-manager/cert-manager/pkg/util/pki FuzzDecodePrivateKeyBytes FuzzDecodePrivateKeyBytes
