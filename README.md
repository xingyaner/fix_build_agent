# OSS-Fuzz Autonomous Fuzz Build Repair Agent

[📊 BuildFixBench Dataset Homepage](https://xingyaner.github.io/BuildFixBench/)

BuildFixBench is a benchmark dataset for reproducing and repairing fuzzing build failures observed in OSS-Fuzz. Each case provides the metadata needed to recreate the failing environment, including the OSS-Fuzz and upstream commit SHAs, archived build log, fuzzing engine, sanitizer, architecture, base-image digest, error category, and—when available—the root-cause commit and workspace.

This project is a high-performance, industrial-grade autonomous agent system designed to fix build errors in **OSS-Fuzz**. It leverages LLMs (DeepSeek) and a multi-agent orchestration framework (**google-adk**) to perform environment locking, root cause analysis, and dual-track (Config vs. Source) code repair.

## 🚀 Core Features

1.  **1+6 Validation Criteria**: A robust verification engine that ensures a fix is truly successful.
    *   **Step 1 (Primary Build)**: Successful generation of executable target binaries.
    *   **Step 6 (Runtime Stability Audit)**: A mandatory **45s critical stability test** ensuring execution speed (exec/s) > 0.
    *   **Steps 2-5 (Quality Metrics)**: Verification of Sanitizer injection (ASan), Engine symbols (libFuzzer/AFL++), Project logic linking, and Shared dependency integrity.
2.  **HAFix (Heuristic History-Enhanced Localization)**: Automatically identifies the "Buggy Commit" by analyzing Git history (`git blame`, `fl_diff`, `fn_pair`) to provide temporal context for the LLM.
3.  **Physical State Tree Rollback**: Utilizes Git to manage physical snapshots of the environment. If a repair path deteriorates (score > 7), the system performs a physical `git reset` to a stable state and clears the previous analysis bias.
4.  **Token-Efficient Memory Management**: 
    *   **Prune Session History**: A whitelist-based strategy that physically wipes intermediate tool call noise (ls, find, read_file) while retaining core reasoning.
    *   **Context Truncation**: Specialized `tail_100_lines` logging and summary agents keep context within the 131k token limit.
5.  **Expert Knowledge RAG-Lite**: Matches build log patterns against a curated `expert_knowledge.json` to inject strategic guidance for complex infrastructure issues (e.g., WORKDIR conflicts).

## Real-World Merged Repairs

| Project | Failure Date | Repair Date | Evidence | Location | Commit |
| --- | --- | --- | --- | --- | --- |
| sigstore | 2025-11-05 | 2025-11-12 | [Issue #2205](https://github.com/sigstore/sigstore/issues/2205); [PR #14254](https://github.com/google/oss-fuzz/pull/14254) | Upstream | [d8ab8af](https://github.com/sigstore/sigstore/commit/d8ab8afb132621efe295937f5178b2d02938bf20) |
| mupdf | 2025-11-08 | 2025-11-19 | [PR #14271](https://github.com/google/oss-fuzz/pull/14271) | OSS-Fuzz | [5ac272f](https://github.com/google/oss-fuzz/commit/5ac272f888ce9e0b8692ec3057356a4ca4de793d) |
| neqo | 2025-11-03 | 2025-11-22 | [Issue #3147](https://github.com/mozilla/neqo/issues/3147) | OSS-Fuzz | [261e9e8](https://github.com/google/oss-fuzz/commit/261e9e8784d317c6697a57e49d0bd914295a32b6) |
| croaring | 2025-10-29 | 2025-11-21 | [PR #763](https://github.com/RoaringBitmap/CRoaring/pull/763) | Upstream | [d5f8433](https://github.com/RoaringBitmap/CRoaring/commit/d5f843304265e41b5d4ae3104f636f28f858c9d8) |
| maven | 2025-11-25 | 2025-12-02 | [PR #14357](https://github.com/google/oss-fuzz/pull/14357) | OSS-Fuzz | [edbab81](https://github.com/google/oss-fuzz/commit/edbab81b93be32075b2af7c45041b48cc17c3234) |
| dgraph | 2025-12-04 | 2025-12-12 | [PR #14462](https://github.com/google/oss-fuzz/pull/14462) | OSS-Fuzz | [2ddf9db](https://github.com/google/oss-fuzz/commit/2ddf9dba55086dca0c392e583b29d60f3e6d2247) |
| airflow | 2024-12-14 | 2025-12-12 | [PR #14326](https://github.com/google/oss-fuzz/pull/14326) | OSS-Fuzz | [966a662](https://github.com/google/oss-fuzz/commit/966a66250cc71a50fcc4577d5efb6e4ecc605eba) |
| xstream | 2025-11-07 | 2025-12-12 | [PR #14284](https://github.com/google/oss-fuzz/pull/14284) | OSS-Fuzz | [88c5240](https://github.com/google/oss-fuzz/commit/88c5240080d7d0a0feede9cfb8290e10741e7c64) |
| pacemaker | 2025-11-27 | 2025-12-13 | [PR #14476](https://github.com/google/oss-fuzz/pull/14476) | OSS-Fuzz | [a07a94b](https://github.com/google/oss-fuzz/commit/a07a94be68415f4b9d99c1e9ce11af50ed809fad) |
| flyway | 2025-12-09 | 2025-12-14 | [PR #14491](https://github.com/google/oss-fuzz/pull/14491) | OSS-Fuzz | [ff36f23](https://github.com/google/oss-fuzz/commit/ff36f234e008e131adfd4f7444d47d8c911c6ef3) |
| mysql-server | 2025-12-04 | 2025-12-18 | [PR #14555](https://github.com/google/oss-fuzz/pull/14555) | OSS-Fuzz | [134af98](https://github.com/google/oss-fuzz/commit/134af986af46f5ab201feb4076a2e1f51758795e) |
| mosquitto | 2025-10-01 | 2025-12-18 | [PR #14516](https://github.com/google/oss-fuzz/pull/14516) | OSS-Fuzz | [783f398](https://github.com/google/oss-fuzz/commit/783f398540c701581c0d6a5e21ff7afb4ba61d73) |
| airflow | 2025-12-18 | 2025-12-23 | [PR #14614](https://github.com/google/oss-fuzz/pull/14614) | OSS-Fuzz | [f4f5e12](https://github.com/google/oss-fuzz/commit/f4f5e12b8a772d8ee39020a888b194cbd9d103ee) |
| pffft | 2026-01-05 | 2026-01-14 | [PR #14776](https://github.com/google/oss-fuzz/pull/14776) | OSS-Fuzz | [722491e](https://github.com/google/oss-fuzz/commit/722491e7bad3b63f61a302a8a04dfa1309875c9f) |
| varnish | 2026-01-05 | 2026-01-14 | [PR #14777](https://github.com/google/oss-fuzz/pull/14777) | OSS-Fuzz | [24a5c10](https://github.com/google/oss-fuzz/commit/24a5c105caf5ad267aca7acb02bce9040cc4ec15) |
| netcdf | 2026-01-05 | 2026-01-14 | [PR #14778](https://github.com/google/oss-fuzz/pull/14778) | OSS-Fuzz | [be6d6d7](https://github.com/google/oss-fuzz/commit/be6d6d7bb385059667b4b20a31d29b0cfcd2c1f5) |
| aptos-core | 2026-01-30 | 2026-05-07 | [Issue #19495](https://github.com/aptos-labs/aptos-core/issues/19495); [PR #19496](https://github.com/aptos-labs/aptos-core/pull/19496) | Upstream | [44a6cf4](https://github.com/aptos-labs/aptos-core/commit/44a6cf440654cef3f124789e88eab7f37f6e8f93) |
| roaring-bitmap | 2025-08-22 | 2026-07-20 | [Issue #838](https://github.com/RoaringBitmap/RoaringBitmap/issues/838); [PR #839](https://github.com/RoaringBitmap/RoaringBitmap/pull/839) | Upstream | [e140aee](https://github.com/RoaringBitmap/RoaringBitmap/commit/e140aee9358b1e33be6e5b0cd8f043daef05299a) |
| sigstore-java | 2025-11-11 | 2026-07-21 | [PR #15891](https://github.com/google/oss-fuzz/pull/15891) | OSS-Fuzz | [d944b51](https://github.com/google/oss-fuzz/commit/d944b51503f8b6cd5a43534a66f616fce41eb5e0) |
| jimfs | 2025-10-28 | 2026-07-21 | [Issue #490](https://github.com/google/jimfs/issues/490); [PR #491](https://github.com/google/jimfs/pull/491) | Upstream | [7b8bcf8](https://github.com/google/jimfs/commit/7b8bcf810dc430a0abcbd84fa92975189b037306) |
| toml_edit | 2025-05-01 | 2026-07-24 | [PR #15896](https://github.com/google/oss-fuzz/pull/15896) | OSS-Fuzz | [c3deaec](https://github.com/google/oss-fuzz/commit/c3deaec3e671bb2289cd1366af9dfc15c131f86c) |
| glaze | 2025-12-19 | 2026-08-03 | [Issue #2742](https://github.com/stephenberry/glaze/issues/2742); [PR #2743](https://github.com/stephenberry/glaze/pull/2743) | Upstream | [PR event](https://github.com/stephenberry/glaze/pull/2752#event-28859172870) |
| bc-gh | Never succeeded | 2026-08-08 | [Issue #103](https://github.com/gavinhoward/bc/issues/103); [PR #104](https://github.com/gavinhoward/bc/pull/104); [PR #15930](https://github.com/google/oss-fuzz/pull/15930) | Upstream | [d16fbee](https://github.com/gavinhoward/bc/commit/d16fbee9f7b6367f6db0e46a9fd7dd3fd6f65b72) |

Notes:

*   `Dockerfile change` and `build.sh change` are classified as `OSS-Fuzz`.
*   `bc-gh` had never successfully completed a fuzz build before the listed repair.

## 📂 Project Structure

```text
.
├── agent.py                 # Main Orchestrator & Loop Logic
├── agent_tools.py           # Core Tools (Git, Build, 1+6 Validation, RAG)
├── oss-fuzz/                # Local OSS-Fuzz infrastructure (Must be at this level)
├── instructions/            # Agent Persona & Workflow Instructions
├── expert_knowledge.json    # Expert Pattern Knowledge Base
├── projects.yaml            # Project Task List (Metadata Source)
├── process/
│   └── project/             # Cloned Target Software (e.g., ./process/project/curl)
│   └── fixed/               # Archived successful repairs with full content
└── agent_logs/              # Real-time console & mirrored event logs
```

## 🛠️ Setup Requirements

*   **Python**: 3.10+
*   **System Utilities**:
    *   **GitHub CLI (`gh`)**: Must be installed and authenticated (`gh auth login`).
    *   **Docker**: Required for OSS-Fuzz containerized builds.
    *   **Standard Tools**: `nm`, `ldd`, `python3`.
*   **API Key**: A valid DeepSeek API key set in a `.env` file.

```bash
# Install required Python libraries
pip install litellm google-adk requests openpyxl pyyaml python-dotenv
```

## ⚙️ Configuration

### 1. Create a `.env` file in the root:
```env
API_KEY='your_api_key_here'
```

### 2. Define tasks in `projects.yaml`:
```yaml
- project: example_project
  sha: <oss_fuzz_commit_sha>
  software_repo_url: <target_git_url>
  software_sha: <target_software_commit_sha>
  base_image_digest: <docker_image_hash>
  engine: libfuzzer
  sanitizer: address
  architecture: x86_64
  fixed_state: 'no'
  state: 'no'
```

## 🔄 Workflow Logic

### Phase 1: Deterministic Setup
`initial_setup_agent` locks the Docker base image digest and checkouts the exact Git SHAs. It enforces `build_mode: source` for local mounting.

### Phase 2: Inner Loop (Max 8-15 Iterations)
*   **Build & 1+6 Audit**: `run_fuzz_and_collect_log_agent` executes the build via `run_fuzz_build_and_validate`.
*   **Decision**: `decision_agent` checks if Step 1 (Build) and Step 6 (Runtime) both passed.
*   **Reflection**: `reflection_agent` assigns a **Deterioration Score (1-10)** based on the 1+6 metrics.
*   **Rollback**: Catastrophic failures trigger a physical environment revert via Git.
*   **Diagnosis (HAFix)**: `commit_finder_agent` locates the buggy commit using temporal or trace analysis.
*   **Solve & Apply**: `fuzzing_solver_agent` generates a multi-file Patch Plan, which is then applied by `solution_applier_agent`.

### Phase 3: Cleanup & Archive
Successful fixes are validated, archived to `process/fixed/` with full content, and the `projects.yaml` report is updated.

## ⚠️ Critical Engagement Rules

*   **Anchor Integrity**: Patching requires an exact byte-for-byte match of the `ORIGINAL` block.
*   **Observability**: All STDOUT and STDERR are mirrored to `agent_logs/` via `StreamTee` for post-mortem debugging.

## 🚀 Execution

```bash
python agent.py
```
