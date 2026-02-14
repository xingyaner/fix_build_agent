import os
import re
import sys
import shutil
import requests
import subprocess
import json
import yaml
import openpyxl
import subprocess
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Set
from google.adk.tools.tool_context import ToolContext


# =================================================================
# --- 消融实验全局开关 (Ablation Global Config) ---
# =================================================================
# 建议：在运行不同版本的实验时，仅需在此处修改布尔值
ENABLE_HISTORY_ENHANCEMENT = True  # 是否开启 HAFix 启发式历史增强
ENABLE_REFLECTION = True        # 是否开启反思学习逻辑
ENABLE_ROLLBACK = True          # 是否开启状态树回退逻辑
ENABLE_EXPERT_KNOWLEDGE = True   # 是否开启专家知识注入
# =================================================================

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
# Build relative path to the process directory
PROCESSED_PROJECTS_DIR = os.path.join(CURRENT_DIR, "process")
PROCESSED_PROJECTS_FILE = os.path.join(PROCESSED_PROJECTS_DIR, "project_processed.txt")

def extract_buggy_line_info(log_path: str) -> List[Dict]:
    """
    【新增】从日志中提取文件名和行号 (例如 parser.c:125)
    """
    if not os.path.exists(log_path): return []
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    # 匹配常见的编译错误格式 filename.c:line:col: error
    pattern = r"([\w\-\./]+\.[a-z]{1,3}):(\d+):"
    matches = re.findall(pattern, content)
    
    results = []
    seen = set()
    for file, line in matches:
        if (file, line) not in seen:
            results.append({"file": file, "line": int(line)})
            seen.add((file, line))
    return results[:3] # 仅取前三个最相关的错误点

def get_enhanced_history_context(project_source_path: str, file_rel_path: str, line_num: int) -> Dict:
    """
    【新增】HAFix 核心实现：提取 fn_all, fn_pair, fl_diff
    """
    print(f"--- Tool: get_enhanced_history_context for {file_rel_path}:{line_num} ---")
    if not ENABLE_HISTORY_ENHANCEMENT:
        return {"status": "skipped", "reason": "History enhancement disabled."}

    original_cwd = os.getcwd()
    try:
        os.chdir(project_source_path)
        
        # 1. 定位提交 (Blame 策略 + 兜底)
        sha = ""
        # 尝试直接 blame
        cmd_blame = ["git", "blame", "-L", f"{line_num},{line_num}", "--porcelain", file_rel_path]
        res = subprocess.run(cmd_blame, capture_output=True, text=True)
        
        if res.returncode == 0:
            sha = res.stdout.split('\n')[0].split(' ')[0]
        else:
            # 不可追溯 Bug 兜底：向上扫描 5 行
            for offset in range(1, 6):
                search_line = max(1, line_num - offset)
                cmd_retry = ["git", "blame", "-L", f"{search_line},{search_line}", "--porcelain", file_rel_path]
                res_retry = subprocess.run(cmd_retry, capture_output=True, text=True)
                if res_retry.returncode == 0:
                    sha = res_retry.stdout.split('\n')[0].split(' ')[0]
                    break
        
        if not sha or len(sha) < 8:
            return {"status": "error", "message": "Could not locate a valid Git blame commit."}

        # 2. 提取 fl_diff (意图补丁)
        diff_patch = subprocess.run(["git", "show", sha, "--pretty=format:%b", "-p"], capture_output=True, text=True).stdout

        # 3. 提取 fn_all (协同演化函数名)
        # 获取该提交中所有修改的文件
        co_files = subprocess.run(["git", "show", "--name-only", "--pretty=format:", sha], capture_output=True, text=True).stdout.split()
        fn_all = f"This commit co-modified these files: {', '.join(co_files)}"

        # 4. 提取 fn_pair (函数演化快照)
        # 简单实现：提取该行附近的 20 行作为演化上下文
        start, end = max(1, line_num - 10), line_num + 10
        fn_before = subprocess.run(["git", "show", f"{sha}^:{file_rel_path}"], capture_output=True, text=True).stdout.splitlines()[start:end]
        fn_after = subprocess.run(["git", "show", f"{sha}:{file_rel_path}"], capture_output=True, text=True).stdout.splitlines()[start:end]

        history_payload = {
            "sha": sha,
            "fl_diff": diff_patch[:5000], # 限制长度
            "fn_all": fn_all,
            "fn_pair": {
                "before": "\n".join(fn_before),
                "after": "\n".join(fn_after)
            }
        }
        return {"status": "success", "data": history_payload}

    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        os.chdir(original_cwd)





def checkout_project_commit(project_source_path: str, sha: str) -> Dict[str, str]:
    """
    在目标软件项目的源代码目录中执行 git checkout 命令。
    """
    print(f"--- Tool: checkout_project_commit called for SHA: {sha} in '{project_source_path}' ---")

    if not os.path.isdir(os.path.join(project_source_path, ".git")):
        return {'status': 'error', 'message': f"The directory '{project_source_path}' is not a git repository."}

    original_path = os.getcwd()
    try:
        os.chdir(project_source_path)

        # 确保仓库处于干净状态，避免 checkout 冲突
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
    下载远程日志文件到本地指定目录，并按 '年_月_日 error.txt' 格式命名。
    例如：build_error_log/aptos-core/2026_1_30 error.txt
    """
    print(f"--- Tool: download_remote_log called for URL: {log_url} ---")

    try:
        # 1. 解析 error_time_str 为日期格式
        try:
            # 尝试处理 YYYY-MM-DD 或 YYYY-M-D
            error_date = datetime.strptime(error_time_str, '%Y-%m-%d').date()
        except ValueError:
            # 备用尝试 YYYY.MM.DD
            error_date = datetime.strptime(error_time_str, '%Y.%m.%d').date()

        # 2. 构建本地存储路径
        local_log_dir = os.path.join("build_error_log", project_name)
        os.makedirs(local_log_dir, exist_ok=True) # 确保项目目录存在

        # 3. 构造本地文件名
        if sys.platform == "win32":
            local_log_filename = error_date.strftime("%Y_%#m_%#d") + " error.txt"
        else:
            local_log_filename = error_date.strftime("%Y_%-m_%-d") + " error.txt"
        
        local_log_filepath = os.path.join(local_log_dir, local_log_filename)

        # 4. 检查文件是否已存在，如果存在则跳过下载
        if os.path.exists(local_log_filepath):
            print(f"--- Log file already exists locally: {local_log_filepath}. Skipping download. ---")
            return {"status": "success", "local_path": os.path.abspath(local_log_filepath), "message": "Log file already exists locally."}

        # 5. 下载日志文件
        print(f"--- Downloading log from {log_url} to {local_log_filepath} ---")
        response = requests.get(log_url, stream=True)
        response.raise_for_status() # 检查HTTP响应状态

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
    strategy_used: str,
    solution_plan: str,
    build_log_tail: str,
    reflection_analysis: str,
    deterioration_score: int,
    should_rollback: bool = False
) -> Dict:
    """
    【反思学习核心工具 - 状态树增强版】
    1. 记录尝试、反思及恶化评分。
    2. 判定是否触发回溯机制（包含连续高分判定）。
    """
    # 如果关闭反思学习，不写磁盘，不返回触发回退的信号
    if not ENABLE_REFLECTION:
        print(f"--- Ablation: Reflection is DISABLED. Skipping journal update for attempt {attempt_id} ---")
        return {
            "status": "success",
            "reflection_summary": "Reflection disabled.",
            "trigger_rollback": False,
            "history_count": 0
        }

    print(f"--- Tool: update_reflection_journal (v2) called for attempt {attempt_id} ---")

    JOURNAL_DIR = "generated_prompt_file"
    JOURNAL_FILE = os.path.join(JOURNAL_DIR, "reflection_journal.json")
    os.makedirs(JOURNAL_DIR, exist_ok=True)

    # 1. 构造当前记录
    new_entry = {
        "attempt_id": attempt_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_used,
        "deterioration_score": deterioration_score,
        "reflection": reflection_analysis,
        "should_rollback": should_rollback
    }

    # 2. 读取历史记录
    history = []
    if os.path.exists(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except:
            history = []

    history.append(new_entry)
    with open(JOURNAL_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    # 3. 判定触发机制：连续两次评分 > 7
    consecutive_high_score = False
    if len(history) >= 2:
        if history[-1].get("deterioration_score", 0) > 7 and history[-2].get("deterioration_score", 0) > 7:
            consecutive_high_score = True
            print("!!! Triggered Rollback: Consecutive high deterioration scores (>7) !!!")

    # 4. 生成用于 State 的摘要
    lessons_learned = [f"Attempt {h['attempt_id']} (Score: {h.get('deterioration_score',0)}): {h['reflection']}" for h in history[-3:]]
    summary_for_state = "\n".join(lessons_learned)

    return {
        "status": "success",
        "reflection_summary": summary_for_state,
        "trigger_rollback": should_rollback or consecutive_high_score,
        "deterioration_score": deterioration_score, # 关键：返回评分
        "history_count": len(history)
    }

def query_expert_knowledge(log_path: str) -> Dict:
    """
    【专家知识检索工具】
    从知识库中提取通用原则，并根据日志匹配特定建议。
    """
    # 如果关闭专家知识，直接返回空结果，不读取 JSON，不进行正则匹配
    if not ENABLE_EXPERT_KNOWLEDGE:
        print("--- Ablation: Expert Knowledge is DISABLED. Returning empty guidance. ---")
        return {"status": "success", "knowledge": "No specific expert knowledge is available for this run."}


    print(f"--- Tool: query_expert_knowledge called for: {log_path} ---")
    KNOWLEDGE_FILE = "expert_knowledge.json"
    
    if not os.path.exists(KNOWLEDGE_FILE):
        return {"status": "error", "message": "Expert knowledge base (JSON) not found."}
    
    try:
        with open(KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
            kb = json.load(f)
        
        # 1. 提取通用原则
        general_info = "\n".join([f"- {item}" for item in kb.get("general_principles", [])])
        
        # 2. 匹配特定模式
        matched_advice = []
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                log_content = f.read()
            for entry in kb.get("patterns", []):
                if re.search(entry["pattern"], log_content, re.IGNORECASE):
                    matched_advice.append(f"- [Specific Match]: {entry['advice']}")
        
        specific_info = "\n".join(matched_advice) if matched_advice else "No specific pattern matches found."
        
        full_knowledge = f"--- General Principles ---\n{general_info}\n\n--- Pattern-based Advice ---\n{specific_info}"
        return {"status": "success", "knowledge": full_knowledge}
            
    except Exception as e:
        return {"status": "error", "message": f"Failed to query knowledge: {str(e)}"}

def manage_git_state(path: str, action: str, message: str = "", commit_sha: str = "") -> Dict:
    """
    【Git 状态管理器】用于实现状态树的保存与回退。
    action: "init", "commit", "rollback"
    """
    print(f"--- Tool: manage_git_state | Action: {action} | Path: {path} ---")
    if not os.path.exists(path):
        return {"status": "error", "message": f"Path {path} does not exist."}

    original_cwd = os.getcwd()
    try:
        os.chdir(path)
        # 初始化检查：如果不是git仓库则初始化
        if not os.path.exists(".git"):
            subprocess.run(["git", "init"], check=True, capture_output=True)
            subprocess.run(["git", "add", "."], check=True)
            subprocess.run(["git", "commit", "-m", "Initial State"], check=True)

        if action == "init":
            return {"status": "success", "message": f"Git initialized in {path}"}

        if action == "commit":
            subprocess.run(["git", "add", "."], check=True)
            # 检查是否有变更
            diff_check = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
            if not diff_check:
                return {"status": "success", "message": "No changes to commit."}
            
            subprocess.run(["git", "commit", "-m", message], capture_output=True, text=True, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            return {"status": "success", "sha": sha, "message": f"State saved: {message}"}

        elif action == "rollback":
            # 默认回退到上一个 commit (HEAD~1)
            target = commit_sha if commit_sha else "HEAD~1"
            # 检查是否有可回退的提交
            check_log = subprocess.run(["git", "rev-list", "--count", "HEAD"], capture_output=True, text=True)
            if int(check_log.stdout.strip()) <= 1:
                return {"status": "error", "message": "Already at the initial state, cannot rollback further."}
            
            subprocess.run(["git", "reset", "--hard", target], check=True)
            subprocess.run(["git", "clean", "-fd"], check=True)
            return {"status": "success", "message": f"Rolled back to {target}"}

    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        os.chdir(original_cwd)


def clear_commit_analysis_state() -> Dict[str, str]:
    """
    删除Commit分析的哨兵文件，以允许 commit_finder_agent 在下一个循环中重新运行。
    这个函数应该在发生回滚时被调用。
    """
    commit_analysis_file = "generated_prompt_file/commit_changed.txt"
    if os.path.exists(commit_analysis_file):
        try:
            os.remove(commit_analysis_file)
            return {"status": "success", "message": f"已清除旧的Commit分析状态。'{commit_analysis_file}' 文件已被移除。"}
        except Exception as e:
            return {"status": "error", "message": f"移除 '{commit_analysis_file}' 失败: {e}"}
    else:
        return {"status": "success", "message": "没有需要清除的Commit分析状态。"}


def extract_build_metadata_from_log(log_path: str) -> Dict:
    """
    【增强版】从原始报错日志中提取构建所需的关键元数据。
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

        # 1. 提取 Base Image Digest
        digest_match = re.search(r'Digest: sha256:([a-f0-9]{64})', content)
        if digest_match:
            metadata['base_image_digest'] = digest_match.group(1)

        # 2. 提取构建配置 (Step #3)
        for line in lines:
            if 'Starting Step #3 - "compile-' in line:
                m = re.search(r'compile-([a-z0-9]+)-([a-z0-9]+)-([a-z0-9_]+)', line)
                if m:
                    metadata['engine'], metadata['sanitizer'], metadata['architecture'] = m.groups()
                break

        # 3. 提取 Git 信息 (Step #2)
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
    【增强版】锁定基础镜像，并移除 git clone 中的 --depth 1 以支持 SHA 切换。
    """
    print(f"--- Tool: patch_project_dockerfile for {project_name} ---")
    dockerfile_path = os.path.join(oss_fuzz_path, "projects", project_name, "Dockerfile")
    if not os.path.exists(dockerfile_path):
        return {'status': 'skip', 'message': 'Dockerfile not found.'}

    try:
        with open(dockerfile_path, 'r') as f:
            content = f.read()
        
        # 1. 替换基础镜像 Digest
        if base_image_digest:
            content = re.sub(r'FROM\s+gcr.io/oss-fuzz-base/base-builder(@sha256:[a-f0-9]+|:[a-z]+)?', 
                             f'FROM gcr.io/oss-fuzz-base/base-builder@sha256:{base_image_digest}', content)
        
        # 2. 【新增修复】移除 Dockerfile 里的 --depth 1 或 --depth=1
        content = content.replace("--depth 1", "").replace("--depth=1", "")
            
        with open(dockerfile_path, 'w') as f:
            f.write(content)
        return {'status': 'success', 'message': 'Dockerfile patched (Digest + Full Clone).'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

def update_yaml_report(file_path: str, row_index: int, result: str) -> Dict[str, str]:
    """
    [New] Updates the 'state' to 'yes' and adds 'fix_result' and 'fix_date' to the YAML file.
    """
    print(f"--- Tool: update_yaml_report called for file '{file_path}', index {row_index} ---")
    try:
        if not os.path.exists(file_path):
             return {'status': 'error', 'message': f"YAML file not found at '{file_path}'."}

        with open(file_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        if row_index < 0 or row_index >= len(data):
            return {'status': 'error', 'message': "Invalid row index provided."}

        # 更新状态
        data[row_index]['state'] = 'yes'
        # 记录修复结果 (Success/Failure)
        data[row_index]['fix_result'] = result
        # 记录修复时间
        data[row_index]['fix_date'] = datetime.now().strftime('%Y-%m-%d')

        # 写回文件
        with open(file_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        message = f"Successfully updated project at index {row_index} in '{file_path}' with result: '{result}'."
        print(message)
        return {'status': 'success', 'message': message}
    except Exception as e:
        message = f"Failed to update YAML file: {e}"
        print(f"--- ERROR: {message} ---")
        return {'status': 'error', 'message': message}


def get_git_commits_around_date(project_source_path: str, error_date: str, count: int = 10) -> Dict:
    """
    Returns metadata for commits within the range [error_date - 1 day, error_date + 1 day].
    Useful to handle timezone differences or build delays.
    """
    print(f"--- Tool: get_git_commits_around_date called. Path: {project_source_path}, Center Date: {error_date} ---")

    if not os.path.isdir(os.path.join(project_source_path, ".git")):
        return {'status': 'error', 'message': "Not a git repository."}

    try:
        # 1. 解析传入的日期
        # 尝试处理 YYYY-MM-DD 或 YYYY-M-D
        try:
            target_dt = datetime.strptime(error_date, '%Y-%m-%d')
        except ValueError:
            # 备用尝试 YYYY.MM.DD
            target_dt = datetime.strptime(error_date, '%Y.%m.%d')

        # 2. 计算时间窗口 (前后各推1天)
        # 例如: error_date=11-03. start=11-02, end=11-04.
        start_date = (target_dt - timedelta(days=1)).strftime('%Y-%m-%d')
        end_date = (target_dt + timedelta(days=1)).strftime('%Y-%m-%d')
        
        print(f"--- Searching commits between {start_date} and {end_date} (inclusive) ---")

        # 3. 构建 Git 命令
        # --since 和 --until 是包含边界的 (inclusive)
        cmd = [
            "git", "log", 
            f"--since={start_date} 00:00:00", 
            f"--until={end_date} 23:59:59", 
            f"-n {count}", 
            "--pretty=format:%H|%cd|%s", 
            "--date=format:%Y-%m-%d %H:%M:%S"
        ]
        
        result = subprocess.run(cmd, cwd=project_source_path, capture_output=True, text=True, check=False)

        commits = []
        lines = result.stdout.strip().split('\n')
        for line in lines:
            if not line: continue
            parts = line.split('|', 2)
            if len(parts) < 3: continue
            sha, date, msg = parts

            # 获取该 commit 修改的文件列表
            cmd_files = ["git", "show", "--name-only", "--format=", sha]
            res_files = subprocess.run(cmd_files, cwd=project_source_path, capture_output=True, text=True, check=False)
            files = [f.strip() for f in res_files.stdout.split('\n') if f.strip()]

            commits.append({
                "sha": sha,
                "date": date,
                "message": msg,
                "files_changed": files
            })

        print(f"--- Found {len(commits)} commits in range. ---")
        return {'status': 'success', 'commits': commits}
    except Exception as e:
        return {'status': 'error', 'message': f"Failed to get commits: {e}"}


def save_commit_diff_to_file(project_name: str, project_source_path: str, sha: str, error_time: str) -> Dict:
    """
    Gets the full diff of a specific SHA and saves it to 'generated_prompt_file/commit_changed.txt'.
    """
    print(f"--- Tool: save_commit_diff_to_file called. SHA: {sha} ---")
    OUTPUT_FILE = "generated_prompt_file/commit_changed.txt"
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    
    try:
        # 获取详细 Diff
        cmd = ["git", "show", sha, "--stat", "-p"]
        result = subprocess.run(cmd, cwd=project_source_path, capture_output=True, text=True, encoding='utf-8', errors='replace')
        
        if result.returncode != 0:
            return {'status': 'error', 'message': result.stderr}

        content = result.stdout
        
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("--- Commit Context Information ---\n")
            f.write(f"Project Name: {project_name}\n")
            f.write(f"Error Report Time: {error_time}\n")
            f.write(f"Selected Commit SHA: {sha}\n")
            f.write("-" * 30 + "\n\n")
            f.write(content)
            
        return {'status': 'success', 'message': f"Saved diff for {sha} to {OUTPUT_FILE}"}
    except Exception as e:
        return {'status': 'error', 'message': f"Error saving diff: {e}"}

def read_projects_from_yaml(file_path: str) -> Dict:
    """
    [Rigorous Version] Reads project information and automatically finds the
    correct error log file using standard datetime comparison.
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
            if entry.get('fixed_state') == 'no':
                project_name = entry.get('project')
                sha = entry.get('oss-fuzz_sha')
                error_time_str = str(entry.get('error_time', ""))
                # --- START MODIFICATION ---
                fuzzing_build_error_log_url = entry.get('fuzzing_build_error_log', "")
                # --- END MODIFICATION ---

                if project_name and sha:
                    # --- 严谨的日期自动关联逻辑 ---
                    log_dir = os.path.join("build_error_log", project_name)
                    original_log_path = ""

                    # --- START MODIFICATION ---
                    # 优先处理远程日志URL
                    if fuzzing_build_error_log_url.startswith("http"):
                        download_result = download_remote_log(fuzzing_build_error_log_url, project_name, error_time_str)
                        if download_result['status'] == 'success':
                            original_log_path = download_result['local_path']
                        else:
                            print(f"  - CRITICAL: Failed to download remote log for {project_name}: {download_result['message']}")
                            # 如果下载失败，仍然尝试在本地查找，或者跳过此项目
                            # 这里选择继续尝试本地查找，但如果原始日志URL存在，则优先使用下载结果
                    else: # 如果不是URL，则按照原有逻辑在本地查找
                        # 现有本地查找逻辑保持不变
                        if os.path.isdir(log_dir):
                            try:
                                y, m, d = map(int, error_time_str.replace('.', '-').split('-'))
                                base_date = datetime(y, m, d)

                                candidates = []
                                for filename in os.listdir(log_dir):
                                    if "error.txt" in filename and re.match(r"\d{4}_\d{1,2}_\d{1,2} error\.txt", filename):
                                        match = re.search(r"(\d{4})_(\d{1,2})_(\d{1,2})", filename)
                                    # 匹配 '年_月_日 error.txt' 格式
                                        if match:
                                            fy, fm, fd = map(int, match.groups())
                                            file_date = datetime(fy, fm, fd)

                                            if file_date >= base_date:
                                                candidates.append((file_date, filename))

                                if candidates:
                                    candidates.sort(key=lambda x: x[0])
                                    best_match = candidates[0][1]
                                    original_log_path = os.path.abspath(os.path.join(log_dir, best_match))
                                    print(f"  - Rigorous Match: {best_match} (>= {error_time_str})")
                            except Exception as e:
                                print(f"  - Warning: Date parsing error for {project_name}: {e}")
                    # --- END MODIFICATION ---

                    project_info = {
                        "project_name": project_name,
                        "sha": str(sha),
                        "row_index": index,
                        "error_time": error_time_str,
                        "original_log_path": original_log_path,
                        # --- START MODIFICATION ---
                        # 将从YAML中读取到的所有元数据也加入到 project_info 中
                        "software_repo_url": entry.get('software_repo_url', ""),
                        "software_sha": entry.get('software_sha', ""),
                        "engine": entry.get('engine', ""),
                        "sanitizer": entry.get('sanitizer', ""),
                        "architecture": entry.get('architecture', ""),
                        "base_image_digest": entry.get('base_image_digest', "")
                        # --- END MODIFICATION ---
                    }
                    # 只有当 original_log_path 成功获取（无论是本地找到还是远程下载）才添加项目
                    if original_log_path:
                        projects_to_run.append(project_info)
                    else:
                        print(f"Warning: Project '{project_name}' skipped due to missing or failed log file retrieval.")
                else:
                    print(f"Warning: Project at index {index} missing core fields (project_name or oss-fuzz_sha). Skipping.")

        print(f"--- Found {len(projects_to_run)} new projects to process. ---")
        return {'status': 'success', 'projects': projects_to_run}
    except Exception as e:
        return {'status': 'error', 'message': f"Failed to read YAML: {e}"}


# Core Tools
def force_clean_git_repo(repo_path: str) -> Dict[str, str]:
    print(f"--- Tool: force_clean_git_repo (v2) called for: {repo_path} ---")

    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return {'status': 'error', 'message': f"Directory '{repo_path}' is not a valid Git repository."}

    original_path = os.getcwd()
    try:
        os.chdir(repo_path)

        # 1. First, switch to the main branch. Using -f or --force can force a switch, but resetting first is safer.
        # 2. Force reset to HEAD, which will discard all modifications in the working directory. This is the most critical step.
        subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, text=True, check=True)

        # 3. Now that the workspace is clean, we can safely switch branches.
        main_branch = "main" if "main" in subprocess.run(["git", "branch", "--list"], capture_output=True, text=True).stdout else "master"
        subprocess.run(["git", "switch", main_branch], capture_output=True, text=True, check=True)

        # 4. Remove all untracked files and directories (e.g., build artifacts, logs).
        subprocess.run(["git", "clean", "-fdx"], capture_output=True, text=True, check=True)

        message = f"Successfully force-cleaned the repository '{repo_path}'. All local changes and untracked files have been removed."
        print(message)
        return {'status': 'success', 'message': message}

    except subprocess.CalledProcessError as e:
        message = f"Failed to force-clean repository '{repo_path}': {e.stderr.strip()}"
        print(f"--- ERROR: {message} ---")
        return {'status': 'error', 'message': message}
    except Exception as e:
        message = f"An unknown error occurred while cleaning the repository: {e}"
        print(f"--- ERROR: {message} ---")
        return {'status': 'error', 'message': message}
    finally:
        os.chdir(original_path)


def get_project_paths(project_name: str) -> Dict[str, str]:
    """
    Generates and returns the standard project_config_path and project_source_path based on the project name.
    """
    print(f"--- Tool: get_project_paths called for: {project_name} ---")
    # Ensure paths are always relative to the parent directory of the current script file (i.e., the project root)
    base_path = os.path.abspath(os.path.join(os.path.dirname(__file__)))

    safe_project_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()

    config_path = os.path.join(base_path, "oss-fuzz", "projects", safe_project_name)
    source_path = os.path.join(base_path, "process", "project", safe_project_name)

    paths = {
        "project_name": project_name,
        "project_config_path": config_path,
        "project_source_path": source_path,
        "max_depth": 1 # Default to getting 1 level of the file tree
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
    [Revised] Updates the "Whether Fix Was Attempted", "Fix Result", and "Fix Date" columns for a specified row in an .xlsx file.
    """
    print(f"--- Tool: update_excel_report called for file '{file_path}', row {row_index} ---")
    try:
        workbook = openpyxl.load_workbook(file_path)
        sheet = workbook.active
        headers = [cell.value for cell in sheet[1]]

        # Dynamically get column indices
        attempted_col_idx = headers.index("是否尝试修复") + 1  # "Whether Fix Was Attempted"
        result_col_idx = headers.index("修复结果") + 1       # "Fix Result"
        date_col_idx = headers.index("修复日期") + 1         # "Fix Date"

        # [Core write-back logic]
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
    [Revised] Reads project information from the specified .xlsx file.
    Only reads rows where "报错是否一致" ("Error Consistency") is "是" ("Yes") and "是否尝试修复" ("Whether Fix Was Attempted") is not "是" ("Yes").
    """
    print(f"--- Tool: read_projects_from_excel called for: {file_path} ---")
    if not os.path.exists(file_path):
        return {'status': 'error', 'message': f"Excel file not found at '{file_path}'."}

    projects_to_run = []
    try:
        workbook = openpyxl.load_workbook(file_path)
        sheet = workbook.active
        headers = [cell.value for cell in sheet[1]]

        # Verify that all required headers are present
        required_headers = ["项目名称", "复现oss-fuzz SHA", "报错是否一致", "是否尝试修复"]
        if not all(h in headers for h in required_headers):
             return {'status': 'error', 'message': f"Excel file is missing one of the required columns: {required_headers}"}

        # Get column indices for later use
        name_idx = headers.index("项目名称")          # "Project Name"
        sha_idx = headers.index("复现oss-fuzz SHA")   # "Reproducible oss-fuzz SHA"
        consistent_idx = headers.index("报错是否一致")   # "Error Consistency"
        attempted_idx = headers.index("是否尝试修复")  # "Whether Fix Was Attempted"

        for row_index, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            # [Core filtering logic]
            if row[consistent_idx] == "是" and row[attempted_idx] != "是": # "Yes"
                project_info = {
                    "project_name": row[name_idx],
                    "sha": str(row[sha_idx]),
                    "row_index": row_index  # Record the row number for easy write-back
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
    【路径安全+全量克隆版】下载仓库工具
    1. 强制路径锁定：第三方库仅允许存放在 process/project/ 下。
    2. 全量克隆：移除 --depth=1，确保 checkout sha 100% 成功。
    3. 缓冲区优化：解决大仓库 RPC 错误。
    """
    import json
    import time
    import subprocess
    import os
    import shutil

    # --- 核心逻辑：路径强制重定向 ---
    current_work_dir = os.getcwd()
    if project_name == "oss-fuzz":
        # oss-fuzz 保持原样（通常在 ./oss-fuzz）
        final_target_dir = os.path.abspath(target_dir)
    else:
        # 强制所有其他项目进入 process/project/ 目录
        safe_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        final_target_dir = os.path.abspath(os.path.join(current_work_dir, "process", "project", safe_name))
        
        if os.path.abspath(target_dir) != final_target_dir:
            print(f"--- Path Security Enforcement: Redirecting download from {target_dir} to {final_target_dir} ---")

    print(f"--- Tool: download_github_repo called for '{project_name}' ---")

    # --- 1. 预检查逻辑：确保 Git 仓库完整性 ---
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

    # 清理非 Git 目录残余
    if os.path.isdir(final_target_dir):
        shutil.rmtree(final_target_dir)
    os.makedirs(os.path.dirname(final_target_dir), exist_ok=True)

    # --- 2. 确定 Repo URL ---
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

    # --- 3. 配置 Git 缓冲区（解决 TLS/RPC 错误） ---
    subprocess.run(["git", "config", "--global", "http.postBuffer", "524288000"])
    subprocess.run(["git", "config", "--global", "http.lowSpeedLimit", "0"])
    subprocess.run(["git", "config", "--global", "http.lowSpeedTime", "999999"])

    # --- 4. 增强重试克隆逻辑 (注意：此处已移除 --depth=1) ---
    max_retries = 3
    for attempt in range(max_retries):
        print(f"--- Download attempt {attempt + 1}/{max_retries} ---")
        try:
            # 执行全量克隆以支持 SHA 切换
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

# Version Reverting Tool
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
    [Revised] Executes a git checkout command in the fixed oss-fuzz directory.
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

# File Operations and Fuzzing Tools
def apply_patch(solution_file_path: str) -> dict:
    """
    【统计增强版】应用补丁并返回修改规模。
    """
    print(f"--- Tool: apply_patch (Robust with Stats) called ---")
    try:
        if not os.path.exists(solution_file_path):
            return {"status": "error", "message": f"Solution file {solution_file_path} not found."}

        with open(solution_file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        patch_blocks = content.split('---=== FILE ===---')[1:]
        if not patch_blocks:
            return {"status": "error", "message": "Invalid patch format."}

        applied_count = 0
        total_lines_changed = 0
        modified_files = set()
        errors = []

        for block in patch_blocks:
            try:
                parts = block.split('---=== ORIGINAL ===---')
                file_path = parts[0].strip()
                content_parts = parts[1].split('---=== REPLACEMENT ===---')
                original_block = content_parts[0].strip("\n\r")
                replacement_block = content_parts[1].strip("\n\r")

                if not os.path.exists(file_path):
                    errors.append(f"File not found: {file_path}")
                    continue

                with open(file_path, 'r', encoding='utf-8') as f:
                    file_content = f.read()

                if original_block in file_content:
                    new_content = file_content.replace(original_block, replacement_block, 1)
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    
                    # 统计修改行数（取替换前后块的行数最大值）
                    total_lines_changed += max(len(original_block.splitlines()), len(replacement_block.splitlines()))
                    modified_files.add(file_path)
                    applied_count += 1
                else:
                    # (保持原有的回显逻辑...)
                    errors.append(f"Match failed for {file_path}...")

            except Exception as e:
                errors.append(f"Error in block: {str(e)}")

        return {
            "status": "success" if not errors else ("partial_success" if applied_count > 0 else "error"),
            "message": f"Applied {applied_count} patches.",
            "modified_files_count": len(modified_files), # 返回修改文件数
            "total_lines_changed": total_lines_changed,   # 返回影响行数
            "errors": errors
        }
    except Exception as e:
        return {"status": "error", "message": f"Critical failure: {str(e)}"}

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


def read_file_content(file_path: str, tail_lines: Optional[int] = None) -> dict:
    """
    【上下文优化版】读取文件内容，并自动进行瘦身以减少 token 数量。
    - 自动剥离常见的许可证头部注释。
    - 对过长的文件进行智能截断（保留开头和结尾）。
    - 接受 tail_lines 参数只读取末尾行。
    """
    print(f"--- Tool: read_file_content (Optimized) called for: {file_path} (tail_lines={tail_lines}) ---")
    
    if not os.path.isfile(file_path):
        return {"status": "error", "message": f"Error: Path '{file_path}' is not a valid file."}
        
    try:
        with open(file_path, "r", encoding="utf-8", errors='ignore') as f:
            lines = f.readlines()

        # 1. 如果指定了 tail_lines，则优先处理
        if tail_lines and isinstance(tail_lines, int) and tail_lines > 0:
            content = "".join(lines[-tail_lines:])
            message = f"Successfully read the last {len(lines[-tail_lines:])} lines from '{file_path}'."
            return {"status": "success", "message": message, "content": content}

        # 2. 自动剥离常见的许可证/版权头部
        # 匹配以 #, /*, // 开头的连续行
        license_header_pattern = re.compile(r"^(#|//|\s*\*).*$", re.MULTILINE)
        content_str = "".join(lines)
        
        # 寻找第一个非注释行
        first_code_line_index = -1
        for i, line in enumerate(lines):
            stripped_line = line.strip()
            if stripped_line and not license_header_pattern.match(line):
                first_code_line_index = i
                break
        
        if first_code_line_index > 5: # 如果头部注释超过5行，就剥离它
            lines = lines[first_code_line_index:]
            print(f"--- Stripped license header ({first_code_line_index} lines) from '{file_path}' ---")

        # 3. 对过长的文件进行智能截断
        MAX_LINES = 400 # 设置一个合理的文件最大行数
        if len(lines) > MAX_LINES:
            head = lines[:MAX_LINES // 2]
            tail = lines[-MAX_LINES // 2:]
            content = "".join(head) + "\n\n... (File content truncated for brevity) ...\n\n" + "".join(tail)
            message = f"File '{file_path}' was too long, content has been truncated."
            print(f"--- Truncated long file '{file_path}' to {MAX_LINES} lines ---")
        else:
            content = "".join(lines)
            message = f"Successfully read the optimized content of '{file_path}'."

        return {"status": "success", "message": message, "content": content}

    except Exception as e:
        return {"status": "error", "message": f"An error occurred while reading file '{file_path}': {str(e)}"}

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

def prompt_generate_tool(project_main_folder_path: str, max_depth: int, config_folder_path: str, expert_knowledge: str = "", enhanced_history: str = "") -> dict:
    """
    【专家知识集成版】自动收集 Fuzzing 上下文信息，确保专家知识被注入。
    """
    print("--- Workflow Tool: prompt_generate_tool started ---")
    PROMPT_DIR = "generated_prompt_file"
    PROMPT_FILE_PATH = os.path.join(PROMPT_DIR, "prompt.txt")
    FILE_TREE_PATH = os.path.join(PROMPT_DIR, "file_tree.txt")
    FUZZ_LOG_PATH = "fuzz_build_log_file/fuzz_build_log.txt"
    COMMIT_DIFF_PATH = os.path.join(PROMPT_DIR, "commit_changed.txt")
    JOURNAL_FILE = os.path.join(PROMPT_DIR, "reflection_journal.json")

    if not os.path.isdir(config_folder_path):
        return {"status": "error", "message": f"Config path '{config_folder_path}' is not a directory."}

    os.makedirs(PROMPT_DIR, exist_ok=True)
    project_name = os.path.basename(os.path.abspath(project_main_folder_path))

    # --- Step 1: 写入初始引导词与专家建议 ---
    with open(PROMPT_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(f"You are a premier expert in software testing. Fix the build for: {project_name}.\n")
        
        if enhanced_history:
            f.write("\n--- 【ENHANCED REPOSITORY HISTORY (HAFix) 】 ---\n")
            f.write(f"{enhanced_history}\n")
            
        if expert_knowledge:
            f.write("\n--- 【EXPERT KNOWLEDGE & STRATEGIC GUIDANCE】 ---\n")
            f.write(f"{expert_knowledge}\n")

    # --- Step 2: 注入历史反思教训 ---
    if ENABLE_REFLECTION and os.path.exists(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, 'r', encoding='utf-8') as f_j:
                history = json.load(f_j)
            if history:
                with open(PROMPT_FILE_PATH, "a", encoding="utf-8") as f_out:
                    f_out.write("\n--- 【LESSONS FROM PREVIOUS ATTEMPTS】 ---\n")
                    for entry in history[-3:]:
                        f_out.write(f"- [Attempt {entry['attempt_id']}] {entry['reflection']}\n")
        except Exception: pass
    else:
        # 如果关闭，即便文件存在也不读取，确保 Solver 拿不到历史经验
        print("--- Ablation: Skipping reflection history injection. ---")

    # --- Step 3: 附加配置文件内容 ---
    all_config_files = [os.path.join(config_folder_path, f) for f in sorted(os.listdir(config_folder_path)) if os.path.isfile(os.path.join(config_folder_path, f))]
    with open(PROMPT_FILE_PATH, "a", encoding="utf-8") as f:
        f.write("\n\n--- Configuration Files (Dockerfile, build.sh, etc.) ---\n")
    for config_file in all_config_files:
        try:
            with open(config_file, "r", encoding="utf-8", errors='ignore') as source_f, open(PROMPT_FILE_PATH, "a", encoding="utf-8") as dest_f:
                dest_f.write(f"\n### Content from: {os.path.basename(config_file)} ###\n")
                dest_f.write(source_f.read())
        except Exception: pass

    # --- Step 4: 生成并附加文件树 ---
    save_file_tree_shallow(project_main_folder_path, max_depth, FILE_TREE_PATH)
    if os.path.exists(FILE_TREE_PATH):
        with open(PROMPT_FILE_PATH, "a", encoding="utf-8") as f:
            f.write("\n\n--- Project File Tree (Shallow View) ---\n")
            with open(FILE_TREE_PATH, "r", encoding="utf-8") as source_f:
                f.write(source_f.read())

    # --- Step 5: 附加最近的 Commit 变更 ---
    if os.path.isfile(COMMIT_DIFF_PATH):
        with open(PROMPT_FILE_PATH, "a", encoding="utf-8") as f:
            f.write("\n\n--- Recent Commit Changes ---\n")
            with open(COMMIT_DIFF_PATH, "r", encoding="utf-8", errors='ignore') as source_f:
                f.write(source_f.read())

    # --- Step 6: 附加构建错误日志 (最后500行) ---
    log_result = read_file_content(FUZZ_LOG_PATH, tail_lines=500)
    if log_result['status'] == 'success':
        with open(PROMPT_FILE_PATH, "a", encoding="utf-8") as f:
            f.write("\n\n--- Fuzz Build Log (Last 500 lines) ---\n")
            f.write(log_result['content'])

    # --- 新增 Step 7: 读取并返回完整内容 ---
    try:
        if os.path.exists(PROMPT_FILE_PATH):
            with open(PROMPT_FILE_PATH, "r", encoding="utf-8") as f:
                full_prompt = f.read()
            
            # 返回 status 为 success，并将内容放在 'content' 字段
            # 这样 Agent 的指令 "Use the return result... as your final output" 才能生效
            return {
                "status": "success", 
                "message": "Prompt generation complete.",
                "content": full_prompt  # 这是关键：确保内容被返回
            }
        else:
            return {"status": "error", "message": "Prompt file was not created successfully."}

    except Exception as e:
        return {"status": "error", "message": f"Failed to read generated prompt: {e}"}


def run_fuzz_build_streaming(
    project_name: str,
    oss_fuzz_path: str,
    sanitizer: str,
    engine: str,
    architecture: str,
    mount_path: Optional[str] = None  # 新增可选参数
) -> dict:
    """
    【增强版】执行 Fuzzing 构建命令。
    如果提供了 mount_path，则使用挂载本地源码的命令格式。
    """
    print(f"--- Tool: run_fuzz_build_streaming (Enhanced) called for project: {project_name} ---")
    if mount_path:
        print(f"--- Build Mode: Source Mount (Path: {mount_path}) ---")
    else:
        print(f"--- Build Mode: Standard Configuration ---")

    LOG_DIR = "fuzz_build_log_file"
    LOG_FILE_PATH = os.path.join(LOG_DIR, "fuzz_build_log.txt")
    os.makedirs(LOG_DIR, exist_ok=True)

    try:
        helper_script_path = os.path.join(oss_fuzz_path, "infra/helper.py")
        
        # 构建基础命令
        command = ["python3.10", helper_script_path, "build_fuzzers"]
        
        # 根据策略调整参数顺序
        # 格式 1 (Config Fix): build_fuzzers --sanitizer ... <project_name>
        # 格式 2 (Source Fix): build_fuzzers <project_name> <source_path> --sanitizer ...
        
        if mount_path:
            # 源码挂载模式：显式指定项目名和路径
            command.append(project_name)
            command.append(mount_path)
        
        # 添加通用参数
        command.extend([
            "--sanitizer", sanitizer, 
            "--engine", engine, 
            "--architecture", architecture
        ])

        # 如果不是挂载模式，项目名通常在最后（或者根据 helper.py 的具体实现，放在中间也可以，但为了保险起见，遵循标准 oss-fuzz 用法）
        # 标准用法通常是: build_fuzzers --args project_name
        if not mount_path:
            command.append(project_name)

        print(f"--- Executing command: {' '.join(command)} ---")

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=oss_fuzz_path,
            encoding='utf-8',
            errors='ignore'
        )

        full_log_content = []
        for line in process.stdout:
            print(line, end='', flush=True)
            full_log_content.append(line)
        
        process.wait()
        return_code = process.returncode
        print("\n--- Fuzzing process finished. ---")

        final_log = "".join(full_log_content)
        
        failure_keywords = [
            "error:", "failed:", "timeout", "timed out", "build failed",
            "no such package", "error loading package", "failed to fetch"
        ]
        
        success_keywords = ["build completed successfully", "successfully built"]
        
        is_truly_successful = True
        
        if return_code != 0:
            is_truly_successful = False
            
        if any(keyword in final_log.lower() for keyword in failure_keywords):
            is_truly_successful = False
            
        if is_truly_successful:
            if not any(keyword in final_log.lower() for keyword in success_keywords):
                if "found 0 targets" in final_log.lower():
                    is_truly_successful = False
        
        # --- 根据判断结果写入文件并返回 ---
        if is_truly_successful:
            content_to_write = "success"
            message = f"Fuzzing build command appears TRULY SUCCESSFUL. Result saved to '{LOG_FILE_PATH}'."
            status = "success"
        else:
            # 如果失败，保存完整的日志
            content_to_write = final_log
            message = f"Fuzzing build command FAILED based on log analysis. Detailed log saved to '{LOG_FILE_PATH}'."
            status = "error"
            
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(content_to_write)
            
        print(message)
        return {"status": status, "message": message}

    except Exception as e:
        message = f"An unknown exception occurred: {str(e)}"
        print(message)
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(message)
        return {"status": "error", "message": message}
