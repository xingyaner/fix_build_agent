import os
import re
import sys
import shutil
import requests
import subprocess
import json
import yaml
import openpyxl
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Set
from google.adk.tools.tool_context import ToolContext

ENABLE_HISTORY_ENHANCEMENT = True
ENABLE_REFLECTION = True
ENABLE_ROLLBACK = True
ENABLE_EXPERT_KNOWLEDGE = True

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

PROCESSED_PROJECTS_DIR = os.path.join(CURRENT_DIR, "process")
PROCESSED_PROJECTS_FILE = os.path.join(PROCESSED_PROJECTS_DIR, "project_processed.txt")


def prune_session_history(tool_context: ToolContext) -> dict:
    """
    Adopt a whitelist strategy to erase intermediate tool call details.
    Retains summary_agent, fuzzing_solver_agent events, and critical diagnostic tool outputs.
    """
    try:
        session = tool_context.session
        if not session or not session.events:
            return {"status": "success", "message": "Memory is already clean."}

        original_count = len(session.events)
        whitelist_authors = ['summary_agent', 'fuzzing_solver_agent']
        protected_tools = {'run_container_diagnostic'}

        new_events = [session.events[0]]

        for event in session.events[1:]:
            # 1. 保留白名单 Agent 的全部事件（含其工具调用与响应）
            if event.author in whitelist_authors:
                new_events.append(event)
            # 2. 显式保留关键诊断工具的响应，防止环境取证证据丢失
            elif hasattr(event, 'get_function_responses'):
                keep_event = False
                for resp in event.get_function_responses():
                    if resp.name in protected_tools:
                        keep_event = True
                        break
                if keep_event:
                    new_events.append(event)
                # 其他非白名单工具响应默认丢弃
            elif not hasattr(event, 'get_function_calls'):
                # 保留非工具调用的普通文本/状态事件
                new_events.append(event)

        session.events.clear()
        for e in new_events:
            session.events.append(e)

        msg = f"Pruned {original_count - len(new_events)} tool call events."
        print(f"--- [MEMORY] {msg} ---")
        return {"status": "success", "message": msg}
    except Exception as e:
        return {"status": "error", "message": f"Memory intervention failed: {str(e)}"}


def extract_buggy_line_info(log_path: str, project_name: str = "", project_source_path: str = "",
                            error_date: str = "") -> dict:
    """
    [HAFix Phase 1 & 2] Dynamic Clue Mining + Self-Healing Identification.
    Replaces the original logic to support multi-mode extraction and path scoring.

    Args:
        log_path: Path to the build log.
        project_name: Name of the project.
        project_source_path: (New) Root path of the source code for path validation.
        error_date: (New) Date string for fallback time-window search.
    """
    import os, re, subprocess
    if not os.path.exists(log_path): return {"status": "error", "message": "Log file not found."}

    # --- Helper: Read tail lines with noise filtering ---
    def read_log_tail(path, count):
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            # Filter out Agent diagnostic noise and common non-error logs
            noise = ["--- Tool:", "RESULT:", "[⚠️", "usage: helper.py", "Step #"]
            return [l for l in lines[-count:] if not any(kw in l for kw in noise)]
        except:
            return []

    # --- Phase 1: Clue Mining ---
    # 1. Scan 500 lines
    content = "".join(read_log_tail(log_path, 500))
    # Regex supports C/C++/Go/Rust/Config files: (path/file.ext:line:)
    pattern = r"([\w\-\./_]+\.(?:c|cpp|h|cc|cxx|rs|go|py|sh|java|swift|cmake|txt)):(\d+):?"
    matches = re.findall(pattern, content)

    # 2. Fallback to 1000 lines if empty
    if not matches:
        content = "".join(read_log_tail(log_path, 1000))
        matches = re.findall(pattern, content)

    # 3. Keyword Fallback for Phase 2 Path B
    if not matches:
        keywords = [kw for kw in ["GOMODCACHE", "WORKDIR", "overlay", "lib.*not found", "undefined reference"] if
                    re.search(kw, content, re.I)]
        return {"status": "success", "clue_type": "keyword", "data": {"keywords": keywords, "error_date": error_date}}

    # --- Phase 2: Path Self-Healing & Scoring ---
    # Pre-load recent changes for scoring (+5 points)
    recent_changes = set()
    if project_source_path and os.path.isdir(os.path.join(project_source_path, ".git")):
        try:
            res = subprocess.run(["git", "-C", project_source_path, "log", "-n", "50", "--name-only", "--format="],
                                 capture_output=True, text=True, timeout=10)
            recent_changes = {f.strip() for f in res.stdout.split('\n') if f.strip()}
        except:
            pass

    scored_candidates = []
    for raw_file, raw_line in matches:
        score, final_path = 0, raw_file

        # Check direct existence (Score 100)
        if project_source_path and os.path.exists(os.path.join(project_source_path, raw_file)):
            score = 100
        else:
            # Attempt to find file via search (Self-Healing)
            basename = os.path.basename(raw_file)
            if project_source_path:
                try:
                    find_cmd = ["find", project_source_path, "-name", basename, "-type", "f"]
                    find_res = subprocess.run(find_cmd, capture_output=True, text=True, timeout=5).stdout.strip().split(
                        '\n')
                    best_s, best_c = -999, None
                    for cand in [c for c in find_res if c]:
                        rel = os.path.relpath(cand, project_source_path)
                        s = 0
                        if os.path.dirname(raw_file) in rel: s += 10  # +10: Parent dir match
                        if rel in recent_changes: s += 5  # +5: Recently modified
                        s -= abs(rel.count('/') - raw_file.count('/'))  # -1: Depth penalty
                        if s > best_s: best_s, best_c = s, rel
                    if best_c: score, final_path = 60 + best_s, best_c
                except:
                    pass

        scored_candidates.append({"file": final_path, "line": int(raw_line), "score": score})

    scored_candidates.sort(key=lambda x: x['score'], reverse=True)
    best = scored_candidates[0] if scored_candidates else None

    # Execute Blame (if score >= 60)
    if best and best['score'] >= 60 and project_source_path:
        try:
            blame_cmd = ["git", "-C", project_source_path, "blame", "-L", f"{best['line']},{best['line']}",
                         "--porcelain", best['file']]
            res = subprocess.run(blame_cmd, capture_output=True, text=True, check=True, timeout=10)
            sha = res.stdout.split('\n')[0].split(' ')[0]
            if len(sha) >= 7:
                return {"status": "success", "clue_type": "blame",
                        "data": {"sha": sha, "file": best['file'], "line": best['line']}}
        except:
            pass

    # Fallback to Time-Window Suspects
    return {"status": "success", "clue_type": "time_window",
            "data": {"file": best['file'] if best else None, "error_date": error_date}}


def get_enhanced_history_context(project_source_path: str, clue_data: dict = None, file_rel_path: str = "",
                                 line_num: int = 0, sha: str = "") -> dict:
    """
    [HAFix Phase 3] Chain-of-Evidence Synthesis.
    Replaces the original logic to support multi-mode evidence gathering.

    Args:
        project_source_path: Root path of the source code.
        clue_data: (New) Structured output from Phase 1 (extract_buggy_line_info).
        file_rel_path: (Legacy/Deprecated) Used if clue_data is missing.
        line_num: (Legacy/Deprecated) Used if clue_data is missing.
        sha: (Legacy/Deprecated) Used if clue_data is missing.
    """
    import os, subprocess
    from datetime import datetime, timedelta

    if not os.path.isdir(os.path.join(project_source_path, ".git")):
        return {"status": "error", "message": "Not a git repository."}

    # --- Auto-convert Legacy Call to Phase 1 Data if clue_data is missing ---
    if not clue_data:
        if sha:
            clue_data = {"clue_type": "blame", "data": {"sha": sha, "file": file_rel_path}}
        elif file_rel_path and line_num:
            clue_data = {"clue_type": "time_window", "data": {"file": file_rel_path}}  # Fallback handling

    if not clue_data:
        return {"status": "error", "message": "No clue data provided."}

    clue_type = clue_data.get("clue_type")
    payload = clue_data.get("data", {})
    evidence = {"clue_type": clue_type, "suspect_sha": payload.get("sha", "N/A"), "core_files": [],
                "auxiliary_timeline": [], "diffs": []}

    try:
        # 1. Determine Core Tracing Files
        if clue_type == "blame":
            target_sha = payload['sha']
            show_res = subprocess.run(
                ["git", "-C", project_source_path, "show", "--name-only", "--format=", target_sha],
                capture_output=True, text=True, timeout=10).stdout
            changed = [f.strip() for f in show_res.split('\n') if f.strip()]
            # Filter to top 3 relevant source/config files
            exts = ('.c', '.go', '.cpp', '.h', '.sh', 'Dockerfile', 'build.sh', 'go.mod', 'CMakeLists.txt')
            evidence["core_files"] = [f for f in changed if f.endswith(exts) or any(x in f for x in exts)][:3]
        else:
            # Keyword/Time-Window mode: prioritize the reported file
            if payload.get("file"):
                evidence["core_files"] = [payload.get("file")]
            # If no file, leave empty for Agent to scan config

        # 2. Build Time Window (±24h)
        error_date = payload.get("error_date", "")
        since_until = []
        if error_date and error_date.strip():
            try:
                clean_date = error_date.replace('.', '-').replace('/', '-')
                t = datetime.strptime(clean_date.split()[0], '%Y-%m-%d')
                since_until = [f"--since={(t - timedelta(days=1)).strftime('%Y-%m-%d')}",
                               f"--until={(t + timedelta(days=1)).strftime('%Y-%m-%d')}"]
            except:
                pass

        # 3. Chain-of-Evidence Collection
        for f in [x for x in evidence["core_files"] if x and os.path.exists(os.path.join(project_source_path, x))]:
            # A. Auxiliary Timeline (git log -n 5)
            log_cmd = ["git", "-C", project_source_path, "log", *since_until, "-n", "5", "--format=%H|%cd|%s", "--", f]
            log_res = subprocess.run(log_cmd, capture_output=True, text=True, timeout=10).stdout.strip()
            if log_res:
                evidence["auxiliary_timeline"].append(
                    {"file": f, "commits": [l.split('|') for l in log_res.split('\n') if '|' in l]})

            # B. Structural Sampling (Unified Diff -U3)
            if evidence["suspect_sha"] != "N/A" and len(evidence["suspect_sha"]) >= 7:
                diff_cmd = ["git", "-C", project_source_path, "show", "-U3", "--format=", evidence["suspect_sha"], "--",
                            f]
                diff_res = subprocess.run(diff_cmd, capture_output=True, text=True, timeout=10).stdout
                evidence["diffs"].append({"file": f, "content": diff_res[:8000]})  # 8000 char Token Guard

        return {"status": "success", "data": evidence}
    except Exception as e:
        return {"status": "error", "message": f"Synthesis failed: {str(e)}"}


def checkout_project_commit(project_source_path: str, sha: str) -> Dict[str, str]:
    """
    Execute git checkout command in the project source directory.
    """
    print(f"--- Tool: checkout_project_commit called for SHA: {sha} in '{project_source_path}' ---")

    if not os.path.isdir(os.path.join(project_source_path, ".git")):
        return {'status': 'error', 'message': f"The directory '{project_source_path}' is not a git repository."}

    original_path = os.getcwd()
    try:
        os.chdir(project_source_path)

        subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, text=True, check=True)
        subprocess.run(["git", "clean", "-fdx"], capture_output=True, text=True, check=True)

        command = ["git", "checkout", sha]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')

        if result.returncode == 0:
            return {'status': 'success', 'message': f"Successfully checked out SHA {sha} in project source."}
        else:
            return {'status': 'error', 'message': f"Git command failed in project source: {result.stderr.strip()}"}
    except Exception as e:
        return {'status': 'error', 'message': f"An unexpected error occurred during project source checkout: {e}"}
    finally:
        os.chdir(original_path)


def download_remote_log(log_url: str, project_name: str, error_time_str: str) -> Dict[str, str]:
    """
    Download remote log file and save it locally using 'YYYY_M_D error.txt' format.
    """
    print(f"--- Tool: download_remote_log called for URL: {log_url} ---")

    try:
        try:
            error_date = datetime.strptime(error_time_str, '%Y-%m-%d').date()
        except ValueError:
            error_date = datetime.strptime(error_time_str, '%Y.%m.%d').date()

        local_log_dir = os.path.join("build_error_log", project_name)
        os.makedirs(local_log_dir, exist_ok=True)

        if sys.platform == "win32":
            local_log_filename = error_date.strftime("%Y_%#m_%#d") + " error.txt"
        else:
            local_log_filename = error_date.strftime("%Y_%-m_%-d") + " error.txt"
        
        local_log_filepath = os.path.join(local_log_dir, local_log_filename)

        if os.path.exists(local_log_filepath):
            print(f"--- Log file already exists locally: {local_log_filepath}. Skipping download. ---")
            return {"status": "success", "local_path": os.path.abspath(local_log_filepath), "message": "Log file already exists locally."}

        print(f"--- Downloading log from {log_url} to {local_log_filepath} ---")
        response = requests.get(log_url, stream=True)
        response.raise_for_status()

        with open(local_log_filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        print(f"--- Successfully downloaded log to: {local_log_filepath} ---")
        return {"status": "success", "local_path": os.path.abspath(local_log_filepath), "message": "Successfully downloaded remote log."}

    except requests.exceptions.RequestException as e:
        return {"status": "error", "message": f"Failed to download log from {log_url}: {e}"}
    except ValueError as e:
        return {"status": "error", "message": f"Invalid error_time_str format '{error_time_str}': {e}"}
    except Exception as e:
        return {"status": "error", "message": f"An unexpected error occurred during log download: {e}"}


def update_reflection_journal(
    project_name: str,
    attempt_id: int,
    round_id: int,
    strategy_used: str,
    solution_plan: str,
    build_log_tail: str,
    reflection_analysis: str,
    deterioration_score: int,
    solved_problems: str,
    unsolved_problems: str,
    should_rollback: bool = False
) -> Dict:
    """
    Explicitly record Attempt and Round IDs, store concise problem descriptions, and extract recent lessons for the state.
    """
    import os
    import json
    from datetime import datetime

    if not os.environ.get("ENABLE_REFLECTION", "True") == "True":
        return {"status": "success", "trigger_rollback": False}

    print(f"--- Tool: update_reflection_journal (v5) for A{attempt_id}_R{round_id} ---")
    JOURNAL_FILE = "reflection_journal.json"

    new_entry = {
        "attempt_id": attempt_id,
        "round_id": round_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_used,
        "solved": solved_problems,
        "unsolved": unsolved_problems,
        "deterioration_score": deterioration_score,
        "reflection": reflection_analysis,
        "should_rollback": should_rollback
    }

    history = []
    if os.path.exists(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except: pass
    history.append(new_entry)

    with open(JOURNAL_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    current_attempt_history = [h for h in history if h['attempt_id'] == attempt_id]
    consecutive_high_score = False
    if len(current_attempt_history) >= 2:
        if current_attempt_history[-1].get("deterioration_score", 0) > 7 and \
           current_attempt_history[-2].get("deterioration_score", 0) > 7:
            consecutive_high_score = True

    lessons = []
    for h in current_attempt_history[-3:]:
        lessons.append(
            f"A{h['attempt_id']}_R{h['round_id']} (Score:{h['deterioration_score']}):\n"
            f"  [Fixed]: {h['solved']}\n"
            f"  [Pending]: {h['unsolved']}"
        )
    summary_for_state = "\n".join(lessons)

    return {
        "status": "success",
        "reflection_summary": summary_for_state,
        "trigger_rollback": should_rollback or consecutive_high_score,
        "deterioration_score": deterioration_score
    }


def query_expert_knowledge(log_path: str) -> dict:
    """
    Dynamically select relevant principles from the knowledge base based on log keywords to optimize token usage.
    """
    if not ENABLE_EXPERT_KNOWLEDGE:
        print("--- [ABLATION] Expert Knowledge is DISABLED. ---")
        return {
            "status": "success",
            "knowledge": "Expert knowledge system is currently disabled by ablation configuration."
        }
    KNOWLEDGE_FILE = "expert_knowledge.json"
    if not os.path.exists(KNOWLEDGE_FILE):
        return {"status": "error", "message": "Knowledge base not found."}

    try:
        with open(KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
            kb = json.load(f)

        log_sample = ""
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as lf:
                log_sample = "".join(lf.readlines()[-100:]).lower()

        category_map = {
            "linker": ["linker", "undefined reference", "symbol", "lib", ".a", ".so", "link"],
            "docker": ["docker", "workdir", "apt-get", "copy", "run", "entrypoint"],
            "swift": ["swift", "package.swift", "spm", "tools-version"],
            "path": ["no such file", "directory", "cannot stat", "path", "mkdir"]
        }

        selected_principles = []
        all_principles = kb.get("general_principles", [])

        hit_categories = [cat for cat, kws in category_map.items() if any(kw in log_sample for kw in kws)]

        for p in all_principles:
            if any(cat in p.lower() for cat in hit_categories):
                selected_principles.append(p)

        if not selected_principles:
            final_principles = all_principles[:3]
        else:
            final_principles = selected_principles[:6]

        matched_advice = []
        for entry in kb.get("patterns", []):
            if re.search(entry["pattern"], log_sample, re.IGNORECASE):
                matched_advice.append(f"- [Specific Match]: {entry['advice']}")

        knowledge_str = "--- Relevant Principles ---\n" + "\n".join([f"- {item}" for item in final_principles])
        if matched_advice:
            knowledge_str += "\n\n--- Targeted Advice ---\n" + "\n".join(matched_advice)

        return {"status": "success", "knowledge": knowledge_str}
    except Exception as e:
        return {"status": "error", "message": f"Expert knowledge error: {str(e)}"}


def manage_git_state(path: str, action: str, message: str = "", commit_sha: str = "") -> Dict:
    """
    Manages the Git state tree with logical fencing and physical auditing.
    It dynamically calculates rollback quotas by identifying "[AGENT_FIX]" markers,
    ensuring the environment never reverts beyond the experimental baseline.
    """
    import os, subprocess
    print(f"--- Tool: manage_git_state | Action: {action} | Path: {path} ---")

    if not os.path.exists(path):
        return {"status": "error", "message": f"Path {path} does not exist."}

    abs_path = os.path.abspath(path)
    framework_root = os.path.dirname(os.path.abspath(__file__))
    if abs_path == framework_root:
        return {"status": "error",
                "message": "CRITICAL: Security Violation. Operations on Agent Framework root are blocked."}

    original_cwd = os.getcwd()
    try:
        uid = os.getuid()
        gid = os.getgid()

        if action in ["init", "commit", "rollback"]:
            try:
                subprocess.run([
                    "docker", "run", "--rm", "-v", f"{abs_path}:/src",
                    "alpine", "chown", "-R", f"{uid}:{gid}", "/src"
                ], capture_output=True, check=True)
            except Exception as e:
                print(f"--- Warning: Permission reclamation failed: {e} ---")

        os.chdir(abs_path)

        if action in ["init", "commit"]:
            if not os.path.exists(".git"):
                subprocess.run(["git", "init"], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "agent@oss-fuzz-repair.com"], check=True)
            subprocess.run(["git", "config", "user.name", "Repair Agent"], check=True)

        if action == "init":
            subprocess.run(["git", "add", "."], check=True)
            has_commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True).returncode == 0
            if not has_commit:
                subprocess.run(["git", "commit", "-m", "[BASELINE] Initial experiment state"], check=True,
                               capture_output=True)
            return {"status": "success", "message": f"Git initialized at Baseline in {path}"}

        if action == "commit":
            subprocess.run(["git", "add", "."], check=True)
            diff_check = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
            if not diff_check:
                return {"status": "success", "message": "No changes to commit."}

            full_message = f"[AGENT_FIX] {message}"
            subprocess.run(["git", "commit", "-m", full_message], capture_output=True, text=True, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            return {"status": "success", "sha": sha, "message": f"State saved: {full_message}"}

        elif action == "rollback":
            count_cmd = ["git", "log", "--grep=\\[AGENT_FIX\\]", "--oneline"]
            res = subprocess.run(count_cmd, capture_output=True, text=True)
            quota = len([l for l in res.stdout.split('\n') if l.strip()])

            if quota <= 0:
                print(f"--- [BLOCK] Rollback denied: Current path {abs_path} is already at Baseline. ---")
                return {
                    "status": "error",
                    "message": "Already at the Initial Baseline of this experiment. No further rollback possible."
                }

            target = commit_sha if commit_sha else "HEAD~1"
            subprocess.run(["git", "reset", "--hard", target], check=True, capture_output=True)
            subprocess.run(["git", "clean", "-fxd"], check=True, capture_output=True)
            return {"status": "success", "message": f"Rolled back 1 step. Remaining Agent Fixes: {quota - 1}"}

    except Exception as e:
        return {"status": "error", "message": f"Git Intervention Failed: {str(e)}"}
    finally:
        os.chdir(original_cwd)


def clear_commit_analysis_state() -> Dict[str, str]:
    """
    Remove the commit analysis sentinel file to allow commit_finder_agent to re-run in the next loop.
    """
    commit_analysis_file = "generated_prompt_file/commit_changed.txt"
    if os.path.exists(commit_analysis_file):
        try:
            os.remove(commit_analysis_file)
            return {"status": "success", "message": f"Cleared old commit analysis state. '{commit_analysis_file}' has been removed."}
        except Exception as e:
            return {"status": "error", "message": f"Failed to remove '{commit_analysis_file}': {e}"}
    else:
        return {"status": "success", "message": "No commit analysis state to clear."}


def extract_build_metadata_from_log(log_path: str) -> Dict:
    """
    Extract critical build metadata from the original error log.
    """
    print(f"--- Tool: extract_build_metadata from {log_path} ---")
    try:
        if not os.path.exists(log_path):
            return {'status': 'error', 'message': 'Log file not found.'}
            
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        lines = content.splitlines()
        metadata = {
            'base_image_digest': '',
            'engine': 'libfuzzer',
            'sanitizer': 'address',
            'architecture': 'x86_64',
            'software_repo_url': '',
            'software_sha': '',
            'dependencies': []
        }

        digest_match = re.search(r'Digest: sha256:([a-f0-9]{64})', content)
        if digest_match:
            metadata['base_image_digest'] = digest_match.group(1)

        for line in lines:
            if 'Starting Step #3 - "compile-' in line:
                m = re.search(r'compile-([a-z0-9]+)-([a-z0-9]+)-([a-z0-9_]+)', line)
                if m:
                    metadata['engine'], metadata['sanitizer'], metadata['architecture'] = m.groups()
                break

        git_pattern = re.compile(r'url: "([^"]+)", rev: "([^"]+)"')
        found_gits = []
        for line in lines:
            if 'Step #2 - "srcmap"' in line:
                match = git_pattern.search(line)
                if match:
                    found_gits.append({'url': match.group(1), 'rev': match.group(2)})

        if found_gits:
            metadata['software_repo_url'] = found_gits[0]['url']
            metadata['software_sha'] = found_gits[0]['rev']
            metadata['dependencies'] = found_gits[1:]

        return {'status': 'success', 'metadata': metadata}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def patch_project_dockerfile(project_name: str, oss_fuzz_path: str, base_image_digest: str) -> Dict:
    """
    Lock the base image digest and remove depth limits from git clones to support SHA switching.
    """
    print(f"--- Tool: patch_project_dockerfile for {project_name} ---")
    dockerfile_path = os.path.join(oss_fuzz_path, "projects", project_name, "Dockerfile")
    if not os.path.exists(dockerfile_path):
        return {'status': 'skip', 'message': 'Dockerfile not found.'}

    try:
        with open(dockerfile_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if base_image_digest:
            pattern = r'(FROM\s+gcr.io/oss-fuzz-base/base-builder[^\s:@]*)'
            replacement = r'\1' + f'@sha256:{base_image_digest}'
            content = re.sub(pattern + r'[^\s]*', replacement, content, flags=re.IGNORECASE)

        content = re.sub(r'--depth[=\s]+1', '', content)

        with open(dockerfile_path, 'w', encoding='utf-8') as f:
            f.write(content)
            
        return {
            'status': 'success', 
            'message': f'Dockerfile patched with digest {base_image_digest[:8]}... and depth limit removed.'
        }
    except Exception as e:
        return {'status': 'error', 'message': f'Failed to patch Dockerfile: {str(e)}'}


def update_yaml_report(file_path: str, row_index: int, result: str) -> Dict[str, str]:
    """
    Update the project status in the YAML report.
    """
    print(f"--- Tool: update_yaml_report called for file '{file_path}', index {row_index} ---")
    try:
        if not os.path.exists(file_path):
             return {'status': 'error', 'message': f"YAML file not found at '{file_path}'."}

        with open(file_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        if row_index < 0 or row_index >= len(data):
            return {'status': 'error', 'message': "Invalid row index provided."}

        data[row_index]['state'] = 'yes'
        data[row_index]['fix_result'] = result
        data[row_index]['fix_date'] = datetime.now().strftime('%Y-%m-%d')

        with open(file_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        message = f"Successfully updated project at index {row_index} in '{file_path}' with result: '{result}'."
        print(message)
        return {'status': 'success', 'message': message}
    except Exception as e:
        message = f"Failed to update YAML file: {e}"
        print(f"--- ERROR: {message} ---")
        return {'status': 'error', 'message': message}


def get_git_commits_around_date(project_source_path: str, error_date: str, max_limit: int = 300) -> Dict:
    """
    Retrieve ALL commits within a ±24h time window for comprehensive pre-screening.
    Optimized: Returns lightweight metadata (SHA/Date/Message) only.
    File changes & diffs are deferred to Phase 3 on-demand extraction to save time & tokens.
    """
    if not ENABLE_HISTORY_ENHANCEMENT:
        print(f"--- [ABLATION] Temporal commit search is DISABLED. ---")
        return {'status': 'success', 'commits': [], 'total_count': 0}

    print(
        f"--- Tool: get_git_commits_around_date (Comprehensive Scan) | Path: {project_source_path} | Date: {error_date} ---")

    if not os.path.isdir(os.path.join(project_source_path, ".git")):
        return {'status': 'error', 'message': "Not a git repository."}

    try:
        # 容错解析日期
        target_dt = None
        if error_date and error_date.strip():
            for fmt in ['%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d']:
                try:
                    target_dt = datetime.strptime(error_date.strip(), fmt)
                    break
                except ValueError:
                    continue

        if target_dt:
            start_date = (target_dt - timedelta(days=1)).strftime('%Y-%m-%d')
            end_date = (target_dt + timedelta(days=1)).strftime('%Y-%m-%d')
            print(f"--- Scanning commits between {start_date} and {end_date} (Limit: {max_limit}) ---")
            cmd = [
                "git", "log",
                f"--since={start_date} 00:00:00",
                f"--until={end_date} 23:59:59",
                f"--max-count={max_limit}",
                "--pretty=format:%H|%cd|%s",
                "--date=format:%Y-%m-%d %H:%M:%S"
            ]
        else:
            print(f"--- Date invalid. Falling back to recent {max_limit} commits. ---")
            cmd = ["git", "log", f"--max-count={max_limit}", "--pretty=format:%H|%cd|%s",
                   "--date=format:%Y-%m-%d %H:%M:%S"]

        result = subprocess.run(cmd, cwd=project_source_path, capture_output=True, text=True, check=False)

        commits = []
        for line in result.stdout.strip().split('\n'):
            if not line: continue
            parts = line.split('|', 2)
            if len(parts) < 3: continue
            sha, date, msg = parts
            # 🔑 仅返回轻量元数据，不在此处触发 git show 查询文件变更
            commits.append({"sha": sha, "date": date, "message": msg})

        print(f"--- Found {len(commits)} commits in window. Ready for Agent pre-screening. ---")
        return {'status': 'success', 'commits': commits, 'total_count': len(commits)}
    except Exception as e:
        return {'status': 'error', 'message': f"Failed to get commits: {e}"}


def save_commit_diff_to_file(project_name: str, project_source_path: str, sha: str, error_time: str):
    """
    Extract recent changes and simplify based on content length to stay within token limits.
    """

    if not ENABLE_HISTORY_ENHANCEMENT:
        print(f"--- [ABLATION] Saving commit diff is DISABLED. ---")
        return {'status': 'error', 'message': 'History enhancement is disabled by ablation configuration.'}

    import os
    import subprocess
    print(f"--- Tool: save_commit_diff_to_file (With Token Guard) for {sha} ---")
    
    TOKEN_GUARD_CHARS = 12000
    OUTPUT_PATH = "generated_prompt_file/commit_changed.txt"
    os.makedirs("generated_prompt_file", exist_ok=True)

    try:
        raw_diff_res = subprocess.run(["git", "-C", project_source_path, "show", sha], 
                                      capture_output=True, text=True, check=True)
        content = raw_diff_res.stdout

        if len(content) > TOKEN_GUARD_CHARS:
            print(f"  - Content length ({len(content)}) exceeds guard. Simplifying...")
            
            lines = content.split('\n')
            simplified = [l for l in lines if l.startswith(('+', '-', '@', 'commit', 'Author', 'Date'))]
            content = "\n".join(simplified)
            
            if len(content) > TOKEN_GUARD_CHARS:
                summary_res = subprocess.run(["git", "-C", project_source_path, "show", "--stat", sha], 
                                             capture_output=True, text=True, check=True)
                content = "--- [DIFF TOO LARGE: Showing Summary Only] ---\n" + summary_res.stdout

        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            f.write(content)
            
        return {"status": "success", "message": f"Saved simplified diff to {OUTPUT_PATH}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def read_projects_from_yaml(file_path: str) -> Dict:
    """
    Read project information, including state field checks and boolean compatibility.
    """
    print(f"--- Tool: read_projects_from_yaml called for: {file_path} ---")
    if not os.path.exists(file_path):
        return {'status': 'error', 'message': f"YAML file not found at '{file_path}'."}

    projects_to_run = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        if not isinstance(data, list):
            return {'status': 'error', 'message': "YAML file must contain a list of projects."}

        for index, entry in enumerate(data):
            fixed_state = str(entry.get('fixed_state', 'no')).lower()
            state = str(entry.get('state', 'no')).lower()

            if fixed_state == 'no' and state == 'no':
                project_name = entry.get('project')
                sha = entry.get('oss-fuzz_sha')
                error_time_str = str(entry.get('error_time', ""))
                fuzzing_build_error_log_url = entry.get('fuzzing_build_error_log', "")

                if project_name and sha:
                    log_dir = os.path.join("build_error_log", project_name)
                    original_log_path = ""

                    if fuzzing_build_error_log_url.startswith("http"):
                        download_result = download_remote_log(fuzzing_build_error_log_url, project_name, error_time_str)
                        if download_result['status'] == 'success':
                            original_log_path = download_result['local_path']
                    
                    if not original_log_path and os.path.isdir(log_dir):
                        try:
                            y, m, d = map(int, error_time_str.replace('.', '-').split('-'))
                            base_date = datetime(y, m, d)
                            candidates = []
                            for filename in os.listdir(log_dir):
                                if "error.txt" in filename and re.match(r"\d{4}_\d{1,2}_\d{1,2} error\.txt", filename):
                                    match = re.search(r"(\d{4})_(\d{1,2})_(\d{1,2})", filename)
                                    if match:
                                        fy, fm, fd = map(int, match.groups())
                                        file_date = datetime(fy, fm, fd)
                                        if file_date >= base_date:
                                            candidates.append((file_date, filename))
                            if candidates:
                                candidates.sort(key=lambda x: x[0])
                                original_log_path = os.path.abspath(os.path.join(log_dir, candidates[0][1]))
                        except Exception: pass

                    if original_log_path:
                        project_info = {
                            "project_name": project_name,
                            "sha": str(sha),
                            "row_index": index,
                            "error_time": error_time_str,
                            "original_log_path": original_log_path,
                            "software_repo_url": entry.get('software_repo_url', ""),
                            "software_sha": entry.get('software_sha', ""),
                            "engine": entry.get('engine', ""),
                            "sanitizer": entry.get('sanitizer', ""),
                            "architecture": entry.get('architecture', ""),
                            "base_image_digest": entry.get('base_image_digest', "")
                        }
                        projects_to_run.append(project_info)
                    else:
                        print(f"Warning: Project '{project_name}' skipped due to missing log file.")
                else:
                    print(f"Warning: Project at index {index} missing core fields. Skipping.")

        print(f"--- Found {len(projects_to_run)} projects to process (Filtered fixed/processed). ---")
        return {'status': 'success', 'projects': projects_to_run}
    except Exception as e:
        return {'status': 'error', 'message': f"Failed to read YAML: {e}"}

def force_clean_git_repo(repo_path: str) -> Dict[str, str]:
    """
    Perform a deep clean of the specified Git repository with automated permission management.
    """
    import os, subprocess
    print(f"--- Tool: force_clean_git_repo (v3) called for: {repo_path} ---")

    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return {'status': 'error', 'message': f"'{repo_path}' is not a valid Git repository."}

    original_path = os.getcwd()
    try:
        abs_repo_path = os.path.abspath(repo_path)
        uid, gid = os.getuid(), os.getgid()

        subprocess.run([
            "docker", "run", "--rm", "-v", f"{abs_repo_path}:/src",
            "alpine", "chown", "-R", f"{uid}:{gid}", "/src"
        ], capture_output=True, check=False)

        os.chdir(abs_repo_path)
        subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, text=True, check=True)
        branch_res = subprocess.run(["git", "branch", "--list"], capture_output=True, text=True)
        main_branch = "main" if "main" in branch_res.stdout else "master"
        subprocess.run(["git", "switch", "-f", main_branch], capture_output=True, text=True, check=True)
        subprocess.run(["git", "clean", "-fxd"], capture_output=True, text=True, check=True)

        return {'status': 'success', 'message': f"Successfully reclaimed and cleaned '{repo_path}'."}
    except Exception as e:
        return {'status': 'error', 'message': f"Deep clean failed: {str(e)}"}
    finally:
        os.chdir(original_path)

def get_project_paths(project_name: str) -> Dict[str, str]:
    """
    Generates and returns the standard project_config_path and project_source_path based on the project name.
    """
    print(f"--- Tool: get_project_paths called for: {project_name} ---")
    base_path = os.path.abspath(os.path.join(os.path.dirname(__file__)))

    safe_project_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()

    config_path = os.path.join(base_path, "oss-fuzz", "projects", safe_project_name)
    source_path = os.path.join(base_path, "process", "project", safe_project_name)

    paths = {
        "project_name": project_name,
        "project_config_path": config_path,
        "project_source_path": source_path,
        "max_depth": 1
    }
    print(f"--- Generated paths: {paths} ---")
    return paths


def save_processed_project(project_name: str) -> Dict[str, str]:
    """
    Appends a processed project name to the project_processed.txt file.
    """
    print(f"--- Tool: save_processed_project called for: {project_name} ---")
    try:
        os.makedirs(PROCESSED_PROJECTS_DIR, exist_ok=True)
        with open(PROCESSED_PROJECTS_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{project_name}\n")
        message = f"Successfully saved '{project_name}' to processed list."
        print(f"--- {message} ---")
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"Failed to save processed project '{project_name}': {e}"
        print(f"--- ERROR: {message} ---")
        return {"status": "error", "message": message}

def update_excel_report(file_path: str, row_index: int, attempted: str, result: str) -> Dict[str, str]:
    """
    Updates the "Whether Fix Was Attempted", "Fix Result", and "Fix Date" columns for a specified row in an .xlsx file.
    """
    print(f"--- Tool: update_excel_report called for file '{file_path}', row {row_index} ---")
    try:
        workbook = openpyxl.load_workbook(file_path)
        sheet = workbook.active
        headers = [cell.value for cell in sheet[1]]

        attempted_col_idx = headers.index("是否尝试修复") + 1
        result_col_idx = headers.index("修复结果") + 1
        date_col_idx = headers.index("修复日期") + 1

        sheet.cell(row=row_index, column=attempted_col_idx, value=attempted)
        sheet.cell(row=row_index, column=result_col_idx, value=result)
        sheet.cell(row=row_index, column=date_col_idx, value=datetime.now().strftime('%Y-%m-%d'))

        workbook.save(file_path)
        message = f"Successfully updated row {row_index} in '{file_path}' with result: '{result}'."
        print(message)
        return {'status': 'success', 'message': message}
    except Exception as e:
        message = f"Failed to update Excel file: {e}"
        print(f"--- ERROR: {message} ---")
        return {'status': 'error', 'message': message}


def read_projects_from_excel(file_path: str) -> Dict:
    """
    Reads project information from the specified .xlsx file.
    Only reads rows where "Error Consistency" is "Yes" and "Whether Fix Was Attempted" is not "Yes".
    """
    print(f"--- Tool: read_projects_from_excel called for: {file_path} ---")
    if not os.path.exists(file_path):
        return {'status': 'error', 'message': f"Excel file not found at '{file_path}'."}

    projects_to_run = []
    try:
        workbook = openpyxl.load_workbook(file_path)
        sheet = workbook.active
        headers = [cell.value for cell in sheet[1]]

        required_headers = ["项目名称", "复现oss-fuzz SHA", "报错是否一致", "是否尝试修复"]
        if not all(h in headers for h in required_headers):
             return {'status': 'error', 'message': f"Excel file is missing one of the required columns: {required_headers}"}

        name_idx = headers.index("项目名称")
        sha_idx = headers.index("复现oss-fuzz SHA")
        consistent_idx = headers.index("报错是否一致")
        attempted_idx = headers.index("是否尝试修复")

        for row_index, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            if row[consistent_idx] == "是" and row[attempted_idx] != "是":
                project_info = {
                    "project_name": row[name_idx],
                    "sha": str(row[sha_idx]),
                    "row_index": row_index
                }
                projects_to_run.append(project_info)

        print(f"--- Found {len(projects_to_run)} new projects to process. ---")
        return {'status': 'success', 'projects': projects_to_run}
    except Exception as e:
        return {'status': 'error', 'message': f"Failed to read or parse Excel file: {e}"}


def run_command(command: str) -> Dict[str, str]:
    """
    Executes a shell command and returns its output. This is a dangerous tool; use with caution.
    """
    print(f"--- Tool: run_command called with: '{command}' ---")
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            check=True,
            encoding='utf-8'
        )
        output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        return {"status": "success", "output": output}
    except subprocess.CalledProcessError as e:
        output = f"Error executing command.\nReturn Code: {e.returncode}\nSTDOUT:\n{e.stdout}\nSTDERR:\n{e.stderr}"
        return {"status": "error", "message": output}
    except Exception as e:
        return {"status": "error", "message": f"An unexpected error occurred: {e}"}

def truncate_prompt_file(file_path: str, max_lines: int = 2000) -> Dict[str, str]:
    """
    Reads a file, and if it exceeds max_lines, truncates it in the middle, keeping the head and tail.
    """
    print(f"--- Tool: truncate_prompt_file called for: {file_path} ---")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        if len(lines) <= max_lines:
            message = "File is within line limits, no truncation needed."
            print(f"--- {message} ---")
            return {"status": "success", "message": message}

        head_count = max_lines // 4
        tail_count = max_lines - head_count

        truncated_content = "".join(lines[:head_count])
        truncated_content += "\n\n... (Content truncated due to context length limit) ...\n\n"
        truncated_content += "".join(lines[-tail_count:])

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(truncated_content)

        message = f"File '{file_path}' was truncated to approximately {max_lines} lines."
        print(f"--- {message} ---")
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"Failed to truncate file '{file_path}': {e}"
        print(f"--- ERROR: {message} ---")
        return {"status": "error", "message": message}

def archive_fixed_project(project_name: str, project_config_path: str) -> Dict[str, str]:
    """
    Archives the configuration directory of a successfully fixed project into a 'success-fix-project' directory.
    """
    print(f"--- Tool: archive_fixed_project called for: {project_name} ---")
    try:
        base_success_dir = "success-fix-project"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_project_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()

        destination_dir = os.path.join(base_success_dir, f"{safe_project_name}_{timestamp}")

        if not os.path.isdir(project_config_path):
            return {"status": "error", "message": f"Source config path does not exist: {project_config_path}"}

        shutil.copytree(project_config_path, destination_dir)

        message = f"Successfully archived config files for '{project_name}' to '{destination_dir}'."
        print(f"--- {message} ---")
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"Failed to archive project '{project_name}': {e}"
        print(f"--- ERROR: {message} ---")
        return {"status": "error", "message": message}

def download_github_repo(project_name: str, target_dir: str, repo_url: Optional[str] = None) -> Dict[str, str]:
    """
    Download a repository with path enforcement and full cloning.
    Redirects non-oss-fuzz projects to process/project/ and performs a full clone to ensure SHA switching compatibility.
    """
    import json
    import time
    import subprocess
    import os
    import shutil

    current_work_dir = os.getcwd()
    if project_name == "oss-fuzz":
        final_target_dir = os.path.abspath(target_dir)
    else:
        safe_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        final_target_dir = os.path.abspath(os.path.join(current_work_dir, "process", "project", safe_name))
        
        if os.path.abspath(target_dir) != final_target_dir:
            print(f"--- Path Security Enforcement: Redirecting download from {target_dir} to {final_target_dir} ---")

    print(f"--- Tool: download_github_repo called for '{project_name}' ---")

    if os.path.isdir(final_target_dir) and os.path.exists(os.path.join(final_target_dir, ".git")):
        if project_name == "oss-fuzz":
            print(f"--- oss-fuzz exists, pulling latest... ---")
            try:
                subprocess.run(["git", "pull"], cwd=final_target_dir, check=True, capture_output=True)
                return {'status': 'success', 'path': final_target_dir, 'message': 'oss-fuzz updated.'}
            except:
                return {'status': 'success', 'path': final_target_dir, 'message': 'oss-fuzz update failed, using local.'}
        else:
            print(f"--- Repo '{project_name}' exists and is a valid git repo. Skipping download. ---")
            return {'status': 'success', 'path': final_target_dir, 'message': 'Repository already exists.'}

    if os.path.isdir(final_target_dir):
        shutil.rmtree(final_target_dir)
    os.makedirs(os.path.dirname(final_target_dir), exist_ok=True)

    final_repo_url = repo_url if repo_url and repo_url.strip() else None
    if not final_repo_url:
        if project_name == "oss-fuzz":
            final_repo_url = "https://github.com/google/oss-fuzz.git"
        else:
            try:
                search_cmd = ["gh", "search", "repos", project_name, "--sort", "stars", "--limit", "1", "--json", "fullName"]
                result = subprocess.run(search_cmd, capture_output=True, text=True, check=True, encoding='utf-8')
                parsed = json.loads(result.stdout.strip())
                if parsed:
                    final_repo_url = f"https://github.com/{parsed[0]['fullName']}.git"
                else:
                    return {'status': 'error', 'message': f"Repo not found for {project_name}"}
            except Exception as e:
                return {'status': 'error', 'message': f"Search failed: {e}"}

    subprocess.run(["git", "config", "--global", "http.postBuffer", "524288000"])
    subprocess.run(["git", "config", "--global", "http.lowSpeedLimit", "0"])
    subprocess.run(["git", "config", "--global", "http.lowSpeedTime", "999999"])

    max_retries = 3
    for attempt in range(max_retries):
        print(f"--- Download attempt {attempt + 1}/{max_retries} ---")
        try:
            clone_cmd = ["git", "clone", final_repo_url, final_target_dir]
            result = subprocess.run(clone_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return {'status': 'success', 'path': final_target_dir, 'message': 'Successfully cloned.'}
            else:
                print(f"--- Attempt {attempt+1} failed: {result.stderr} ---")
        except Exception as e:
            print(f"--- Attempt {attempt+1} exception: {e} ---")
        time.sleep(10 * (attempt + 1))

    return {'status': 'error', 'message': f"Failed to download {project_name} after {max_retries} attempts."}

def find_sha_for_timestamp(commits_file_path: str, error_date: str) -> Dict[str, str]:
    """
    Finds the most suitable commit SHA for a given date from a commits file.
    """
    print(f"--- Tool: find_sha_for_timestamp called for date: {error_date} ---")
    try:
        target_date = datetime.strptime(error_date, '%Y.%m.%d').date()
    except ValueError:
        return {'status': 'error', 'message': f"Invalid target date format: '{error_date}'. Expected 'YYYY.MM.DD'."}

    todays_commits: List[Tuple[datetime, str]] = []
    past_commits: List[Tuple[datetime, str]] = []

    try:
        with open(commits_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("Time: ") and i + 1 < len(lines) and lines[i+1].strip().startswith("- SHA: "):
                try:
                    timestamp_str = line.replace("Time: ", "")
                    commit_datetime = datetime.strptime(timestamp_str, '%Y.%m.%d %H:%M')
                    sha = lines[i+1].strip().replace("- SHA: ", "")
                    commit_date = commit_datetime.date()
                    if commit_date == target_date:
                        todays_commits.append((commit_datetime, sha))
                    elif commit_date < target_date:
                        past_commits.append((commit_datetime, sha))
                except (ValueError, IndexError):
                    pass
            i += 1
    except FileNotFoundError:
        return {'status': 'error', 'message': f"Commits file not found at: {commits_file_path}"}
    except Exception as e:
        return {'status': 'error', 'message': f"An unexpected error occurred: {e}"}

    if todays_commits:
        earliest_today = min(todays_commits)
        found_sha = earliest_today[1]
        return {'status': 'success', 'sha': found_sha}
    elif past_commits:
        latest_in_past = max(past_commits)
        found_sha = latest_in_past[1]
        return {'status': 'success', 'sha': found_sha}
    else:
        return {'status': 'error', 'message': f"No suitable SHA found on or before the date {error_date}."}


def checkout_oss_fuzz_commit(sha: str) -> Dict[str, str]:
    """
    Executes a git checkout command in the fixed oss-fuzz directory.
    """
    base_path = os.path.abspath(os.path.join(os.path.dirname(__file__)))
    oss_fuzz_path = os.path.join(base_path, "oss-fuzz")
    print(f"--- Tool: checkout_oss_fuzz_commit called for SHA: {sha} in '{oss_fuzz_path}' ---")

    if not os.path.isdir(os.path.join(oss_fuzz_path, ".git")):
        return {'status': 'error', 'message': f"The directory '{oss_fuzz_path}' is not a git repository."}

    original_path = os.getcwd()
    try:
        os.chdir(oss_fuzz_path)
        main_branch = "main" if "main" in subprocess.run(["git", "branch"], capture_output=True, text=True).stdout else "master"
        subprocess.run(["git", "switch", main_branch], capture_output=True, text=True)

        command = ["git", "checkout", sha]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')

        if result.returncode == 0:
            return {'status': 'success', 'message': f"Successfully checked out SHA {sha}."}
        else:
            return {'status': 'error', 'message': f"Git command failed: {result.stderr.strip()}"}
    except Exception as e:
        return {'status': 'error', 'message': f"An unexpected error occurred during checkout: {e}"}
    finally:
        os.chdir(original_path)


def apply_patch(solution_file_path: str) -> dict:
    """
    Apply code patches and return detailed feedback.
    Optimized: Added whitespace-tolerant fuzzy matching for ORIGINAL block alignment.
    """
    import os, difflib, re
    print(f"--- Tool: apply_patch (with Feedback) called ---")

    # 🔑 优化 1：将辅助函数移至循环外，避免重复定义
    def normalize_whitespace(text: str) -> str:
        if not text: return ""
        lines = text.splitlines()
        return '\n'.join(re.sub(r'[ \t]+', ' ', line.rstrip()) for line in lines)

    try:
        if not os.path.exists(solution_file_path):
            return {"status": "error", "message": "Solution file not found."}
        with open(solution_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        patch_blocks = content.split('---=== FILE ===---')[1:]

        applied_count, total_lines_changed = 0, 0
        modified_files = set()
        errors = []

        for block in patch_blocks:
            parts = block.split('---=== ORIGINAL ===---')
            file_path = parts[0].strip()
            content_parts = parts[1].split('---=== REPLACEMENT ===---')
            original_block = content_parts[0].strip("\n\r")
            replacement_block = content_parts[1].strip("\n\r")

            # 🔑 优化 2：增加空块防护
            if not original_block:
                errors.append(f"Empty ORIGINAL block for: {file_path}")
                continue

            if not os.path.exists(file_path):
                errors.append(f"File not found: {file_path}")
                continue
            with open(file_path, 'r', encoding='utf-8') as f:
                file_content = f.read()

            # --- Attempt 1: Exact byte-for-byte match ---
            if original_block in file_content:
                new_content = file_content.replace(original_block, replacement_block, 1)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                total_lines_changed += max(len(original_block.splitlines()), len(replacement_block.splitlines()))
                modified_files.add(file_path)
                applied_count += 1
                continue

            # --- Attempt 2: Whitespace-tolerant fuzzy match ---
            original_normalized = normalize_whitespace(original_block)
            file_normalized = normalize_whitespace(file_content)

            if original_normalized in file_normalized:
                original_lines = original_block.splitlines()
                file_lines = file_content.splitlines()
                orig_len = len(original_lines)

                match_start = None
                # 滑动窗口匹配（常规文件 <5000 行时性能无瓶颈）
                for i in range(len(file_lines) - orig_len + 1):
                    if normalize_whitespace('\n'.join(file_lines[i:i + orig_len])) == original_normalized:
                        match_start = i
                        break

                if match_start is not None:
                    new_lines = file_lines[:match_start] + replacement_block.splitlines() + file_lines[
                                                                                            match_start + orig_len:]
                    new_content = '\n'.join(new_lines)
                    if file_content.endswith('\n') and not new_content.endswith('\n'):
                        new_content += '\n'
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    total_lines_changed += max(orig_len, len(replacement_block.splitlines()))
                    modified_files.add(file_path)
                    applied_count += 1
                    continue

            # --- Match failed: Return actual context for Agent debugging ---
            lines = file_content.splitlines()
            search_anchor = original_block.splitlines()[0].strip()
            matches = difflib.get_close_matches(search_anchor, lines, n=1, cutoff=0.3)

            actual_context = "Unknown context (File may be too different)"
            if matches:
                idx = lines.index(matches[0])
                actual_context = "\n".join(lines[max(0, idx - 5):min(len(lines), idx + 10)])

            errors.append(
                f"MATCH FAILED for {file_path}.\n"
                f"### ACTUAL CONTENT AROUND TARGET AREA ###\n"
                f"{actual_context}\n"
                f"### TIP: Ensure ORIGINAL block matches EXACTLY (whitespace-tolerant matching was attempted) ###"
            )

        return {
            "status": "success" if not errors else ("partial_success" if applied_count > 0 else "error"),
            "modified_files_count": len(modified_files),
            "total_lines_changed": total_lines_changed,
            "errors": errors
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

def save_file_tree(directory_path: str, output_file: Optional[str] = None) -> dict:
    """
    Gets the file tree structure of a specified directory path and saves it to a file.
    """
    print(f"--- Tool: save_file_tree called for path: {directory_path} ---")
    if not os.path.isdir(directory_path):
        error_message = f"Error: The provided path '{directory_path}' is not a valid directory."
        print(error_message)
        return {"status": "error", "message": error_message}
    if output_file is None:
        output_dir = "generated_prompt_file"
        final_output_path = os.path.join(output_dir, "file_tree.txt")
    else:
        final_output_path = output_file
    output_dir = os.path.dirname(final_output_path)
    try:
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        tree_lines = []
        def _build_tree_recursive(path, prefix=""):
            entries = sorted([e for e in os.listdir(path) if not e.startswith('.')])
            pointers = ["├── "] * (len(entries) - 1) + ["└── "]
            for pointer, entry in zip(pointers, entries):
                full_path = os.path.join(path, entry)
                if os.path.isdir(full_path):
                    tree_lines.append(f"{prefix}{pointer}📁 {entry}")
                    extension = "│   " if pointer == "├── " else "    "
                    _build_tree_recursive(full_path, prefix + extension)
                else:
                    tree_lines.append(f"{prefix}{pointer}📄 {entry}")
        tree_lines.insert(0, f"📁 {os.path.basename(os.path.abspath(directory_path))}")
        _build_tree_recursive(directory_path, prefix="")
        with open(final_output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(tree_lines))
        success_message = f"File tree has been successfully generated and saved to '{final_output_path}'."
        print(success_message)
        return {"status": "success", "message": success_message}
    except Exception as e:
        error_message = f"An error occurred while generating or saving the file tree: {str(e)}"
        print(error_message)
        return {"status": "error", "message": error_message}

def save_file_tree_shallow(directory_path: str, max_depth: int, output_file: Optional[str] = None) -> dict:
    """
    Gets the top N levels of the file tree structure for a specified directory and overwrites it to a file.
    """
    print(f"--- Tool: save_file_tree_shallow called for path: {directory_path} with max_depth: {max_depth} ---")
    if not os.path.isdir(directory_path):
        error_message = f"Error: The provided path '{directory_path}' is not a valid directory."
        print(error_message)
        return {"status": "error", "message": error_message}
    if output_file is None:
        output_dir = "generated_prompt_file"
        final_output_path = os.path.join(output_dir, "file_tree.txt")
    else:
        final_output_path = output_file
    output_dir = os.path.dirname(final_output_path)
    try:
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        tree_lines = []
        def _build_tree_recursive(path, prefix="", depth=0):
            if depth >= max_depth:
                return
            try:
                entries = sorted([e for e in os.listdir(path) if not e.startswith('.')])
            except OSError:
                entries = []
            pointers = ["├── "] * (len(entries) - 1) + ["└── "]
            for pointer, entry in zip(pointers, entries):
                full_path = os.path.join(path, entry)
                if os.path.isdir(full_path):
                    tree_lines.append(f"{prefix}{pointer}📁 {entry}")
                    extension = "│   " if pointer == "├── " else "    "
                    _build_tree_recursive(full_path, prefix + extension, depth + 1)
                else:
                    tree_lines.append(f"{prefix}{pointer}📄 {entry}")
        tree_lines.insert(0, f"📁 {os.path.basename(os.path.abspath(directory_path))}")
        _build_tree_recursive(directory_path, prefix="")
        with open(final_output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(tree_lines))
        success_message = f"The top {max_depth} levels of the file tree have been successfully generated and saved to '{final_output_path}'."
        print(success_message)
        return {"status": "success", "message": success_message}
    except Exception as e:
        error_message = f"An error occurred while generating or saving the shallow file tree: {str(e)}"
        print(error_message)
        return {"status": "error", "message": error_message}

def find_and_append_file_details(directory_path: str, search_keyword: str, output_file: Optional[str] = None) -> dict:
    """
    Finds a file or directory by its name or partial path and appends its detailed structure to a file.
    """
    print(f"--- Tool: find_and_append_file_details called for path: {directory_path} with keyword: '{search_keyword}' ---")
    if not os.path.isdir(directory_path):
        error_message = f"Error: The provided path '{directory_path}' is not a valid directory."
        print(error_message)
        return {"status": "error", "message": error_message}
    if output_file is None:
        output_dir = "generated_prompt_file"
        final_output_path = os.path.join(output_dir, "file_tree.txt")
    else:
        final_output_path = output_file
    output_dir = os.path.dirname(final_output_path)
    try:
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        found_paths = []
        for root, dirs, files in os.walk(directory_path):
            all_entries = dirs + files
            for entry in all_entries:
                full_path = os.path.join(root, entry)
                if search_keyword in full_path:
                    found_paths.append(full_path)
        found_paths = sorted(list(set(found_paths)))
        if not found_paths:
            message = f"No file or directory matching '{search_keyword}' was found in '{directory_path}'."
            print(message)
            with open(final_output_path, "a", encoding="utf-8") as f:
                f.write(f"\n\n--- Detailed query result for '{search_keyword}' ---\n")
                f.write(message)
            return {"status": "success", "message": message}
        details_to_append = [f"\n\n--- Detailed query result for '{search_keyword}' ---"]
        for path in found_paths:
            relative_path = os.path.relpath(path, directory_path)
            details_to_append.append(f"\n# Matched path: {relative_path}")
            if os.path.isdir(path):
                def _build_tree_recursive(sub_path, prefix=""):
                    try:
                        entries = sorted([e for e in os.listdir(sub_path) if not e.startswith('.')])
                    except OSError:
                        entries = []
                    pointers = ["├── "] * (len(entries) - 1) + ["└── "]
                    for pointer, entry in zip(pointers, entries):
                        details_to_append.append(f"{prefix}{pointer}{'📁' if os.path.isdir(os.path.join(sub_path, entry)) else '📄'} {entry}")
                _build_tree_recursive(path)
            else:
                details_to_append.append(f"📄 {os.path.basename(path)}")
        with open(final_output_path, "a", encoding="utf-8") as f:
            f.write("\n".join(details_to_append))
        success_message = f"Detailed search results for '{search_keyword}' have been appended to '{final_output_path}'."
        print(success_message)
        return {"status": "success", "message": success_message}
    except Exception as e:
        error_message = f"An error occurred while finding and appending file details: {str(e)}"
        print(error_message)
        return {"status": "error", "message": error_message}


def read_file_content(file_path: str, mode: str = "full") -> dict:
    """
    Read file content with mode support.
    Optimized: Clear error messages with path guidance when file not found.
    """
    import os
    print(f"--- Tool: read_file_content (Mode: {mode}) called for: {file_path} ---")

    # --- Path Pre-check: Return clear error + guidance if file not found ---
    if not os.path.exists(file_path):
        path_guidance = (
            "\n【PATH GUIDANCE】\n"
            "• OSS-Fuzz project configs (Dockerfile, build.sh, project.yaml):\n"
            "  → /fix_build_agent/oss-fuzz/projects/<project_name>/\n"
            "• Third-party source code:\n"
            "  → /fix_build_agent/process/project/<project_name>/\n"
            "• Build logs:\n"
            "  → fuzz_build_log_file/fuzz_build_log.txt\n"
            "• Generated prompts / commit analysis:\n"
            "  → generated_prompt_file/\n"
            "Please verify the absolute path and retry."
        )
        return {
            "status": "error",
            "message": f"File not found: {file_path}{path_guidance}"
        }

    # --- Original read logic (unchanged) ---
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        if mode == "tail_30":
            lines = content.splitlines()
            content = '\n'.join(lines[-30:])
        elif mode == "tail_50":
            lines = content.splitlines()
            content = '\n'.join(lines[-50:])
        elif mode == "tail_100_lines":
            lines = content.splitlines()
            content = '\n'.join(lines[-100:])
        # mode == "full": return full content

        return {
            "status": "success",
            "message": f"Read {len(content.splitlines())} lines from {file_path} (Mode: {mode})",
            "content": content
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to read {file_path}: {str(e)}"}


def create_or_update_file(file_path: str, content: str) -> dict:
    """
    Creates a new file and writes content to it, or overwrites an existing file.
    """
    print(f"--- Tool: create_or_update_file called for path: {file_path} ---")
    try:
        directory = os.path.dirname(file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        message = f"File '{file_path}' has been successfully created/updated."
        print(message)
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"An error occurred while creating or updating file '{file_path}': {str(e)}"
        print(message)
        return {"status": "error", "message": message}

def append_file_to_file(source_path: str, destination_path: str) -> dict:
    """
    Reads the entire content of a source file and appends it to the end of a destination file.
    """
    print(f"--- Tool: append_file_to_file called. Source: '{source_path}', Destination: '{destination_path}' ---")
    if not os.path.isfile(source_path):
        return {"status": "error", "message": f"Error: Source file '{source_path}' does not exist or is not a valid file."}
    if os.path.isdir(destination_path):
        return {"status": "error", "message": f"Error: Destination path '{destination_path}' is a directory and cannot be an append target."}
    if os.path.abspath(source_path) == os.path.abspath(destination_path):
        return {"status": "error", "message": "Error: Source and destination files cannot be the same."}
    try:
        with open(source_path, "r", encoding="utf-8") as f_source:
            content_to_append = f_source.read()
        dest_directory = os.path.dirname(destination_path)
        if dest_directory:
            os.makedirs(dest_directory, exist_ok=True)
        with open(destination_path, "a", encoding="utf-8") as f_dest:
            f_dest.write(content_to_append)
        return {"status": "success", "message": f"Successfully appended the content of '{source_path}' to '{destination_path}'."}
    except Exception as e:
        return {"status": "error", "message": f"An unknown error occurred while appending the file: {str(e)}"}

def append_string_to_file(file_path: str, content: str) -> dict:
    """
    Appends a string of content to the end of a specified file.
    """
    print(f"--- Tool: append_string_to_file called for path: {file_path} ---")
    try:
        directory = os.path.dirname(file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(content)
        return {"status": "success", "message": f"Content successfully appended to file '{file_path}'."}
    except Exception as e:
        return {"status": "error", "message": f"An error occurred while appending content to file '{file_path}': {str(e)}"}

def delete_file(file_path: str) -> dict:
    """
    Deletes a specified file.
    """
    print(f"--- Tool: delete_file called for path: {file_path} ---")
    if not os.path.exists(file_path):
        message = f"Error: File '{file_path}' does not exist and cannot be deleted."
        print(message)
        return {"status": "error", "message": message}
    try:
        os.remove(file_path)
        message = f"File '{file_path}' has been successfully deleted."
        print(message)
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"An error occurred while deleting file '{file_path}': {str(e)}"
        print(message)
        return {"status": "error", "message": message}


def prompt_generate_tool(project_main_folder_path: str, max_depth: int, config_folder_path: str, attempt_id: int,
                         expert_knowledge: str = "", enhanced_history: str = "",
                         validation_report: dict = None) -> dict:
    """
    Aggregates source code, configuration files, expert knowledge, and build validation results into a single prompt file. 
    Implements a prioritized loading strategy and dynamic content degradation to stay within a global context budget.
    """
    import os, re
    from agent_tools import read_file_content, save_file_tree_shallow, truncate_prompt_file

    print(f"--- Workflow Tool: prompt_generate_tool started (Attempt: {attempt_id}) ---")
    PROMPT_DIR = "generated_prompt_file"
    PROMPT_FILE_PATH = os.path.join(PROMPT_DIR, "prompt.txt")
    FUZZ_LOG_PATH = "fuzz_build_log_file/fuzz_build_log.txt"

    GLOBAL_CHAR_BUDGET = 280000
    current_used = 0

    context_stream = expert_knowledge + enhanced_history
    if os.path.exists(FUZZ_LOG_PATH):
        try:
            with open(FUZZ_LOG_PATH, 'r', encoding='utf-8', errors='ignore') as lf:
                context_stream += "".join(lf.readlines()[-50:])
        except:
            pass

    candidates = re.findall(r"([\w\-\./]+\.(?:c|cpp|h|cc|swift|sh|py|java))", context_stream)
    l1_filenames = set([os.path.basename(c) for c in candidates])

    if not os.path.isdir(config_folder_path):
        return {"status": "error", "message": f"Config path error: {config_folder_path}"}

    os.makedirs(PROMPT_DIR, exist_ok=True)
    project_name = os.path.basename(os.path.abspath(project_main_folder_path))

    with open(PROMPT_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(f"Testing Expert. Project: {project_name}. Attempt: {attempt_id}\n")

        # 1. 注入结构化验证报告（已前置，确保 Agent 优先看到判定结果）
        if validation_report:
            f.write("\n--- 【LAST BUILD VALIDATION (1+2+6 CRITERIA)】 ---\n")
            s1 = validation_report.get('step_1_official_list', 'N/A')
            s2 = validation_report.get('step_2_infra_compliance', 'N/A')
            s6 = validation_report.get('step_6_runtime_stability', 'N/A')
            f.write(f"MANDATORY - Step 1 (Official Targets): {s1}\n")
            f.write(f"MANDATORY - Step 2 (Infra Compliance): {s2}\n")
            f.write(f"MANDATORY - Step 6 (Runtime Stability): {s6}\n")

            refs = []
            for k in ["step_3_sanitizer_injected", "step_4_engine_control", "step_5_logic_linkage"]:
                status = validation_report.get(k, 'N/A')
                refs.append(f"{k.split('_')[1]}: {status}")
            if refs:
                f.write("REFERENCE METRICS: " + "; ".join(refs) + "\n")

        f.write(f"\n【ENHANCED HISTORY】\n{enhanced_history}\n")
        f.write(f"\n【STRATEGIC KNOWLEDGE】\n{expert_knowledge}\n")

        all_configs = sorted(os.listdir(config_folder_path))

        for fname in [cfg for cfg in all_configs if cfg in l1_filenames]:
            res = read_file_content(os.path.join(config_folder_path, fname), mode="full")
            c = res.get('content', '')
            f.write(f"\n### {fname} (Priority High) ###\n{c}\n")
            current_used += len(c)

        for fname in [cfg for cfg in all_configs if
                      cfg not in l1_filenames and (cfg.endswith('.sh') or 'Dockerfile' in cfg)]:
            mode = "full" if current_used < (GLOBAL_CHAR_BUDGET * 0.6) else "tail_50"
            res = read_file_content(os.path.join(config_folder_path, fname), mode=mode)
            c = res.get('content', '')
            f.write(f"\n### {fname} (Mode: {mode}) ###\n{c}\n")
            current_used += len(c)

        for fname in [cfg for cfg in all_configs if
                      cfg not in l1_filenames and not cfg.endswith('.sh') and 'Dockerfile' not in cfg]:
            if current_used > GLOBAL_CHAR_BUDGET:
                f.write(f"\n### {fname} ###\n[Content omitted: Context budget full]\n")
            else:
                res = read_file_content(os.path.join(config_folder_path, fname), mode="tail_30")
                c = res.get('content', '')
                f.write(f"\n### {fname} (tail_30) ###\n{c}\n")
                current_used += len(c)

        save_file_tree_shallow(project_main_folder_path, 1, os.path.join(PROMPT_DIR, "file_tree.txt"))

        # 2. 双区日志提取：分离原始编译报错与末尾验证审计表，防止信息遮蔽
        if os.path.exists(FUZZ_LOG_PATH):
            try:
                with open(FUZZ_LOG_PATH, 'r', encoding='utf-8', errors='ignore') as lf:
                    full_log = lf.read()
                # 定位验证报告分隔符
                val_marker = "--- 1+6 VALIDATION SUMMARY"
                if val_marker in full_log:
                    build_context, audit_context = full_log.split(val_marker, 1)
                    f.write(f"\n\n--- BUILD LOG CONTEXT (Errors above this line) ---\n")
                    # 安全截断编译日志，保留尾部足够上下文供正则匹配
                    f.write(build_context[-12000:] if len(build_context) > 12000 else build_context)
                    f.write(f"\n{val_marker}{audit_context}")
                else:
                    f.write(f"\n\n--- BUILD LOG TAIL ---\n{full_log[-12000:]}")
            except Exception:
                f.write(f"\n\n--- BUILD LOG TAIL ---\n[Log read failed]")

    truncate_prompt_file(PROMPT_FILE_PATH, max_lines=2500)
    try:
        with open(PROMPT_FILE_PATH, "r", encoding="utf-8") as rf:
            full_content = rf.read()
        clean_content = "".join(c for c in full_content if c.isprintable() or c in '\n\r\t')
        return {"status": "success", "content": clean_content}
    except Exception as e:
        return {"status": "error", "message": f"Final prompt read error: {str(e)}"}


def _auto_discover_project_symbols(binary_path: str, project_name: str) -> Optional[List[str]]:
    """Heuristically identify project-specific symbols using nm."""
    import subprocess
    try:
        result = subprocess.run(['nm', '-D', binary_path], capture_output=True, text=True, errors='ignore')
        if result.returncode != 0:
            result = subprocess.run(['nm', binary_path], capture_output=True, text=True, errors='ignore')

        lines = result.stdout.splitlines()
        keywords = [project_name.lower(), "deflate", "inflate", "adler32", "crc32"] if project_name == "zlib" else [
            project_name.lower()]
        boilerplate = ('__asan', '__lsan', '__ubsan', '__sanitizer', 'fuzzer::', 'LLVM', 'afl_', '_Z', 'std::')

        candidates = []
        for line in lines:
            parts = line.split()
            if not parts: continue
            symbol = parts[-1]
            if any(kw in symbol.lower() for kw in keywords) and not symbol.startswith(boilerplate):
                candidates.append(symbol)
        return candidates[:5] if candidates else None
    except:
        return None


def _cleanup_environment(oss_fuzz_path: str, project_name: str):
    """Clean up residual containers and release file handles prior to build."""
    import subprocess, os, time, errno
    print(f"[*] Pre-build cleanup for project: {project_name}")
    try:
        subprocess.run(f"docker ps -q --filter \"ancestor=gcr.io/oss-fuzz/{project_name}\" | xargs -r docker kill",
                       shell=True, capture_output=True)
        subprocess.run("docker ps -q --filter \"ancestor=gcr.io/oss-fuzz-base/base-runner\" | xargs -r docker kill",
                       shell=True, capture_output=True)
    except:
        pass

    out_dir = os.path.join(oss_fuzz_path, "build", "out", project_name)
    if os.path.exists(out_dir):
        for i in range(3):
            busy = False
            try:
                for f in os.listdir(out_dir):
                    if not f.endswith(('.so', '.a', '.zip', '.dict', '.options', '.txt')):
                        f_path = os.path.join(out_dir, f)
                        if os.path.isfile(f_path):
                            try:
                                os.remove(f_path)
                            except OSError as e:
                                if e.errno == errno.ETXTBSY: busy = True
                if not busy: break
                time.sleep(2)
            except:
                pass


def run_fuzz_build_and_validate(
        project_name: str,
        oss_fuzz_path: str,
        sanitizer: str,
        engine: str,
        architecture: str,
        mount_path: Optional[str] = None
) -> dict:
    """
    Build and validate fuzzers using official OSS-Fuzz infrastructure.
    Success Criteria: Step 1 (list_fuzzers), Step 2 (check_build), and Step 6 (run_fuzzer) must PASS.
    Reference Criteria: Step 3, 4, 5 are recorded for diagnostic purposes only.
    Log Strategy: Raw build log preserved + Validation Summary appended + RESULT: success/failed marker.
    """
    import os, sys, subprocess, time, signal, re
    print(f"--- Tool: run_fuzz_build_and_validate (Official 1+6) called for: {project_name} ---")
    _cleanup_environment(oss_fuzz_path, project_name)

    LOG_DIR = "fuzz_build_log_file"
    LOG_FILE_PATH = os.path.join(LOG_DIR, "fuzz_build_log.txt")
    os.makedirs(LOG_DIR, exist_ok=True)

    # 1+6 审计报告初始化
    report = {
        "step_1_official_list": "pending",  # 硬性
        "step_2_infra_compliance": "pending",  # 硬性
        "step_3_sanitizer_injected": "pending",  # 参考
        "step_4_engine_control": "pending",  # 参考
        "step_5_logic_linkage": "pending",  # 参考
        "step_6_runtime_stability": "pending"  # 硬性
    }

    try:
        helper_path = os.path.join(oss_fuzz_path, "infra/helper.py")

        # --- Phase 1: Physical Build ---
        build_cmd = ["python3", helper_path, "build_fuzzers"]
        if mount_path: build_cmd.extend([project_name, mount_path])
        build_cmd.extend(["--sanitizer", sanitizer, "--engine", engine, "--architecture", architecture])
        if not mount_path: build_cmd.append(project_name)

        process = subprocess.Popen(
            build_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=oss_fuzz_path
        )
        full_log = []
        for line in process.stdout:
            print(line, end='', flush=True)
            full_log.append(line)
        process.wait()
        final_log = "".join(full_log)

        # 基础编译失败判定（快速失败，不进入深验）
        if process.returncode != 0 or any(k in final_log.lower() for k in ["error:", "failed:", "build failed"]):
            with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
                f.write(final_log + "\n\nRESULT: failed (compilation error)")
            return {"status": "error", "message": "Compilation failed", "validation_report": report}

        # --- Phase 2: Official 1+2+6 Deep Validation ---
        print(f"\n--- [Phase 2] Deep Validation (Official Suite) ---")

        # Step 1: 官方产物识别 (list_fuzzers)
        list_cmd = ["python3", helper_path, "list_fuzzers", project_name]
        list_res = subprocess.run(list_cmd, capture_output=True, text=True, timeout=60, cwd=oss_fuzz_path)
        targets = [l.strip().lstrip('./') for l in list_res.stdout.splitlines()
                   if l.strip() and not l.startswith(('-', '#', '['))]

        if list_res.returncode == 0 and targets:
            report["step_1_official_list"] = f"pass: {len(targets)} target(s)"
            primary_target = targets[0]
        else:
            report["step_1_official_list"] = "fail: no recognized fuzzers"
            primary_target = None

            out_dir = os.path.join(oss_fuzz_path, "build", "out", project_name)
            if os.path.exists(out_dir):
                auxiliary_tools = {"llvm-symbolizer", "clang", "clang++", "llvm-cov"}
                for f in os.listdir(out_dir):
                    fpath = os.path.join(out_dir, f)
                    if f not in auxiliary_tools and os.path.isfile(fpath) and os.access(fpath, os.X_OK):
                        report["step_1_official_list"] = f"pass (physical): {f}"
                        report["physical_artifacts_found"] = True
                        primary_target = f  # Enable subsequent steps to use this target
                        print(f"  [Physical Scan] Found fuzzer binary: {f}")
                        break

        # Step 2: 基础设施合规性 (check_build)
        # 严格对齐 build_fuzzers 参数，单次 30 分钟硬超时（提前完成则立即返回）
        check_cmd = [
            "python3", helper_path, "check_build", project_name,
            "--sanitizer", sanitizer,
            "--engine", engine,
            "--architecture", architecture
        ]
        try:
            check_res = subprocess.run(check_cmd, capture_output=True, text=True, timeout=1800, cwd=oss_fuzz_path)
            report[
                "step_2_infra_compliance"] = "pass" if check_res.returncode == 0 else f"fail: {check_res.stderr.strip()[:100]}"
        except subprocess.TimeoutExpired:
            report["step_2_infra_compliance"] = "fail: check_build timeout (exceeded 30m)"
        except Exception as e:
            report["step_2_infra_compliance"] = f"fail: {str(e)}"

        # Step 3-5: 参考项审计 (nm 符号分析)
        if primary_target:
            target_path = os.path.join(oss_fuzz_path, "build", "out", project_name, primary_target)
            if os.path.exists(target_path):
                try:
                    nm_res = subprocess.run(['nm', target_path], capture_output=True, text=True, errors='ignore')
                    nm_stdout = nm_res.stdout
                except Exception:
                    nm_res = subprocess.run(
                        ["python3", helper_path, "shell", project_name, "-c", f"nm /out/{primary_target}"],
                        capture_output=True, text=True, errors='ignore'
                    )
                    nm_stdout = nm_res.stdout

                report["step_3_sanitizer_injected"] = "pass" if "__asan" in nm_stdout else "warning: missing asan"
                report["step_4_engine_control"] = "pass" if (
                            "LLVMFuzzerRunDriver" in nm_stdout or "__afl_" in nm_stdout) else "warning: engine symbols"
                report["step_5_logic_linkage"] = "pass" if _auto_discover_project_symbols_from_content(nm_stdout,
                                                                                                       project_name) else "warning: logic linkage"
            else:
                for s in ["step_3_sanitizer_injected", "step_4_engine_control", "step_5_logic_linkage"]:
                    report[s] = "skip: binary not accessible"
        else:
            for s in ["step_3_sanitizer_injected", "step_4_engine_control", "step_5_logic_linkage"]:
                report[s] = "skip: no primary target"

        # Step 6: 压力测试稳定性 (run_fuzzer + 早停)
        has_rate = False
        if primary_target and report["step_2_infra_compliance"].startswith("pass"):
            print(f"[*] Starting 45s stability test for: {primary_target}")
            run_cmd = [sys.executable, helper_path, "run_fuzzer", "--engine", engine, "--sanitizer", sanitizer,
                       project_name, primary_target]
            if engine == "libfuzzer": run_cmd.extend(["--", "-max_total_time=30"])

            stability_proc = subprocess.Popen(
                run_cmd, cwd=oss_fuzz_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, preexec_fn=os.setsid
            )
            start_time = time.time()
            try:
                while time.time() - start_time < 45:
                    line = stability_proc.stdout.readline()
                    if line and any(kw in line for kw in ["exec/s:", "corp:", "exec speed"]):
                        has_rate = True
                        print(f"[*] Early stop: detected execution rate.")
                        break
                    if not line and stability_proc.poll() is not None:
                        break
            finally:
                try:
                    os.killpg(os.getpgid(stability_proc.pid), signal.SIGKILL)
                except:
                    pass
                stability_proc.wait()

            report["step_6_runtime_stability"] = "pass" if has_rate else "fail: 0 exec/s or crash"
        else:
            report["step_6_runtime_stability"] = "fail: skipped"

        # --- 最终判定逻辑 (仅 1/2/6 硬性通过) ---
        is_success = (
                report["step_1_official_list"].startswith("pass") and
                report["step_2_infra_compliance"].startswith("pass") and
                report["step_6_runtime_stability"].startswith("pass")
        )

        # 构造审计汇总表格
        summary_table = "\n" + "=" * 50 + "\n1+6 VALIDATION SUMMARY\n" + "-" * 50 + "\n"
        for i, (k, v) in enumerate(report.items(), 1):
            marker = "[MANDATORY]" if i in [1, 2, 6] else "[REFERENCE]"
            summary_table += f"Step {i:<4} {marker:<12} | {v}\n"
        summary_table += "=" * 50 + "\n"
        print(summary_table)

        # 写入物理日志：原始日志 + 审计摘要 + 最终标志
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(final_log)
            f.write(summary_table)
            f.write(f"\nRESULT: {'success' if is_success else 'failed'}\n")

        return {
            "status": "success" if is_success else "error",
            "message": f"Validation {'PASSED' if is_success else 'FAILED'}",
            "validation_report": report
        }

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(f"Exception during validation:\n{str(e)}\n{tb}")
        return {"status": "error", "message": str(e), "validation_report": report}


def _auto_discover_project_symbols_from_content(nm_stdout: str, project_name: str) -> bool:
    """Helper to analyze nm output without re-running the command. Lightweight & regex-safe."""
    keywords = [project_name.lower(), "deflate", "inflate", "adler32", "crc32"] if project_name == "zlib" else [
        project_name.lower()]
    boilerplate = ('__asan', '__lsan', '__ubsan', '__sanitizer', 'fuzzer::', 'LLVM', 'afl_', '_Z', 'std::')

    for line in nm_stdout.splitlines():
        parts = line.split()
        if not parts: continue
        symbol = parts[-1]
        if any(kw in symbol.lower() for kw in keywords) and not symbol.startswith(boilerplate):
            return True
    return False


def run_container_diagnostic(oss_fuzz_path: str, project_name: str, command: str) -> dict:
    """
    Execute a diagnostic command inside the OSS-Fuzz build container using docker run directly.
    Bypasses helper.py limitations (no -c support) for reliable environment probing.
    """
    import os, subprocess, re
    print(f"--- Tool: run_container_diagnostic | Project: {project_name} | Cmd: {command} ---")

    # 1. 安全沙箱拦截
    dangerous_patterns = ['rm -rf /', 'sudo ', 'chmod ', 'chown ', 'dd ', 'mkfs', 'curl ', 'wget ', 'apt-get ',
                          'pip install', '> /dev/', 'mktemp']
    if any(p in command.lower() for p in dangerous_patterns):
        return {"status": "error", "message": "Command blocked by security policy. Read-only diagnostic commands only."}

    # 2. 构造 docker run 命令（使用项目镜像，若构建失败回退至基础镜像）
    image_name = f"gcr.io/oss-fuzz/{project_name}"
    base_image = "gcr.io/oss-fuzz-base/base-builder-go"  # 适配 cert-manager 等项目

    cmd = ["docker", "run", "--rm", "--entrypoint", "/bin/bash", image_name, "-c", command]

    try:
        # 尝试运行项目镜像
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        # 如果镜像不存在（通常因构建中途失败导致未打 tag），回退至基础镜像
        if "Unable to find image" in res.stderr or "No such image" in res.stderr:
            print(f"  - Image '{image_name}' not found. Fallback to base builder for diagnostics.")
            cmd[5] = base_image  # Replace image_name with base_image
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        # 清理 ANSI 码
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        raw_output = (res.stdout + "\n" + res.stderr).strip()
        clean_output = ansi_escape.sub('', raw_output)

        # Token 安全截断
        MAX_CHARS = 8000
        truncated = False
        if len(clean_output) > MAX_CHARS:
            clean_output = clean_output[:MAX_CHARS] + "\n\n[⚠️ OUTPUT TRUNCATED: Exceeded 8000 char safety limit]"
            truncated = True

        return {
            "status": "success" if res.returncode == 0 else "warning",
            "return_code": res.returncode,
            "output": clean_output,
            "truncated": truncated
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Command execution timed out after 60s."}
    except Exception as e:
        return {"status": "error", "message": f"Container diagnostic failed: {str(e)}"}