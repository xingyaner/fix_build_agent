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
# 在运行不同版本的实验时，仅需在此处修改布尔值
ENABLE_HISTORY_ENHANCEMENT = True  # 是否开启启发式历史增强根因定位
ENABLE_REFLECTION = True        # 是否开启反思学习逻辑
ENABLE_ROLLBACK = True          # 是否开启状态树回退逻辑
ENABLE_EXPERT_KNOWLEDGE = True   # 是否开启专家知识注入
# =================================================================

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
# Build relative path to the process directory
PROCESSED_PROJECTS_DIR = os.path.join(CURRENT_DIR, "process")
PROCESSED_PROJECTS_FILE = os.path.join(PROCESSED_PROJECTS_DIR, "project_processed.txt")


def prune_session_history(tool_context: ToolContext) -> dict:
    """
    【物理手术版 v4 - 全量替换】
    采用白名单策略：彻底抹除所有中间过程的工具调用细节（ls, find, read_file_content）。
    仅保留：最初输入、来自 summary_agent 的压缩记忆、以及来自 solver 的补丁计划。
    """
    try:
        session = tool_context.session
        if not session or not session.events:
            return {"status": "success", "message": "Memory is already clean."}

        original_count = len(session.events)
        # 白名单：必须保留的事件
        # 1. 初始消息 (index 0)
        # 2. 总结代理的消息 (承载核心记忆)
        # 3. 求解代理的消息 (承载最近的 patch 逻辑)
        whitelist_authors = ['summary_agent', 'fuzzing_solver_agent']

        new_events = [session.events[0]]  # 物理保留初始指令

        for event in session.events[1:]:
            # 保留关键代理的逻辑输出
            if event.author in whitelist_authors:
                new_events.append(event)
            # 剔除所有包含工具调用 (Tool Call/Response) 的中间冗余
            elif hasattr(event, 'get_function_calls') or hasattr(event, 'get_function_responses'):
                continue
            else:
                # 保留其他非工具调用的控制流事件
                new_events.append(event)

        # 物理覆盖底层的 ADK events 列表
        session.events.clear()
        for e in new_events:
            session.events.append(e)

        msg = f"Surgical Intervention Successful: Pruned {original_count - len(new_events)} tool call events."
        print(f"--- [MEMORY] {msg} ---")
        return {"status": "success", "message": msg}
    except Exception as e:
        return {"status": "error", "message": f"Memory intervention failed: {str(e)}"}

def extract_buggy_line_info(log_path: str, project_name: str = "") -> List[Dict]:
    """
    【路径感知增强版】
    从日志中提取文件名和行号，并自动处理 Docker 路径前缀（如 /src/project_name/）。
    """
    if not os.path.exists(log_path): return []
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
        content = "".join(lines[-2000:])
    
    # 匹配模式：支持大部分编程语言后缀
    pattern = r"([\w\-\./]+\.(?:c|cpp|h|cc|rs|go|py|sh|java)):(\d+):"
    matches = re.findall(pattern, content)
    
    results = []
    seen = set()
    # 构造 Docker 内部路径的各种可能性
    prefixes_to_strip = ["/src/" + project_name + "/", "/src/", "./"]
    
    for file_path, line in matches:
        clean_path = file_path
        # 路径归一化：将 /src/glslang/parser.c 转换为 parser.c
        for prefix in prefixes_to_strip:
            if clean_path.startswith(prefix):
                clean_path = clean_path[len(prefix):]
                break
        
        if (clean_path, line) not in seen:
            results.append({"file": clean_path, "line": int(line)})
            seen.add((clean_path, line))
            
    return results[:3]


def get_enhanced_history_context(project_source_path: str, file_rel_path: str, line_num: int) -> dict:
    """
    【精简化 HAFix v3 - 全量替换】
    1. fn_all 摘要化：若修改文件数 > 6，仅保留前 3 和后 3，并提取公共前缀。
    2. fn_pair 极简采样：正则剥离空行与纯符号行，限制变更展示总量。
    """
    import os
    import subprocess
    import re
    print(f"--- Tool: get_enhanced_history_context (Dehydrated) for {file_rel_path}:{line_num} ---")

    if not os.path.exists(project_source_path):
        return {"status": "error", "message": "Source path not found."}

    try:
        # Step 1: 锁定引发变更的 SHA
        blame_cmd = ["git", "-C", project_source_path, "blame", "-L", f"{line_num},{line_num}", "--porcelain",
                     file_rel_path]
        blame_res = subprocess.run(blame_cmd, capture_output=True, text=True, check=True)
        buggy_sha = blame_res.stdout.split('\n')[0].split(' ')[0]

        if not buggy_sha or len(buggy_sha) < 7:
            return {"status": "error", "message": "Could not identify buggy SHA."}

        # Step 2: 提取并摘要化受影响文件清单 (fn_all)
        files_res = subprocess.run(["git", "-C", project_source_path, "show", "--name-only", "--format=", buggy_sha],
                                   capture_output=True, text=True, check=True)
        all_files = [f.strip() for f in files_res.stdout.split('\n') if f.strip()]

        if len(all_files) > 6:
            summary_files = all_files[:3] + [f"...(skipped {len(all_files) - 6} files)..."] + all_files[-3:]
            # 提取公共前缀以辅助理解
            common_prefix = os.path.commonpath([f for f in all_files if '/' in f]) if len(all_files) > 1 else ""
            fn_all_str = f"Total {len(all_files)} files modified. Common path: {common_prefix}\n" + "\n".join(
                summary_files)
        else:
            fn_all_str = "\n".join(all_files)

        # Step 3: 提取函数级压缩快照 (fn_pair)
        # 使用 -U0 强制零背景上下文
        pair_res = subprocess.run(
            ["git", "-C", project_source_path, "show", "-U0", "--format=", buggy_sha, "--", file_rel_path],
            capture_output=True, text=True, check=True)

        # 过滤：保留 +/- 开头，且剥离纯符号行（如单独的 } 或 [）
        compressed_lines = []
        for line in pair_res.stdout.split('\n'):
            if line.startswith('+') or line.startswith('-'):
                pure_content = line[1:].strip()
                # 忽略长度小于2或全是标点符号的行
                if len(pure_content) > 1 and not re.match(r'^[{}()\[\],;.\s]+$', pure_content):
                    compressed_lines.append(line)

        fn_pair = "\n".join(compressed_lines[:12])  # 最终限制在 12 行最关键的逻辑变更

        history_content = (
            f"--- 精简化根因追踪报告 ---\n"
            f"嫌疑提交: {buggy_sha}\n"
            f"受影响文件清单:\n{fn_all_str}\n"
            f"关键逻辑变更 (仅显示功能行):\n{fn_pair}\n"
            f"--------------------------"
        )
        return {"status": "success", "data": {"sha": buggy_sha, "history_content": history_content}}
    except Exception as e:
        return {"status": "error", "message": str(e)}


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
    【反思工具 v5 - 结构化版】
    1. 显式记录大循环(Attempt)与内循环(Round)ID。
    2. 存储“已解决”与“待解决”问题的精简描述。
    3. 仅提取当前大循环(Attempt)的教训返回给 State。
    """
    import os
    import json
    from datetime import datetime

    if not os.environ.get("ENABLE_REFLECTION", "True") == "True":
        return {"status": "success", "trigger_rollback": False}

    print(f"--- Tool: update_reflection_journal (v5) for A{attempt_id}_R{round_id} ---")
    JOURNAL_FILE = "reflection_journal.json"

    # 1. 构造当前条目
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

    # 2. 读取并追加记录
    history = []
    if os.path.exists(JOURNAL_FILE):
        try:
            with open(JOURNAL_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except: pass
    history.append(new_entry)

    with open(JOURNAL_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    # 3. 判定触发机制：仅检查【本轮大循环】内的连续恶化
    current_attempt_history = [h for h in history if h['attempt_id'] == attempt_id]
    consecutive_high_score = False
    if len(current_attempt_history) >= 2:
        if current_attempt_history[-1].get("deterioration_score", 0) > 7 and \
           current_attempt_history[-2].get("deterioration_score", 0) > 7:
            consecutive_high_score = True

    # 4. 生成用于 State 的摘要（仅限本次大循环内容）
    lessons = []
    # 获取本次大循环最近的 3 条记录
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
    【专家知识动态注入版 - 全量替换】
    根据日志关键字动态筛选最相关的 3-5 条准则，避免全量注入导致的 Token 浪费。
    """
    KNOWLEDGE_FILE = "expert_knowledge.json"
    if not os.path.exists(KNOWLEDGE_FILE):
        return {"status": "error", "message": "Knowledge base not found."}

    try:
        with open(KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
            kb = json.load(f)

        # 提取日志最后 100 行作为关键词扫描区
        log_sample = ""
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as lf:
                log_sample = "".join(lf.readlines()[-100:]).lower()

        # 定义关键词到准则类别的映射（基于您专家库中的常见术语）
        category_map = {
            "linker": ["linker", "undefined reference", "symbol", "lib", ".a", ".so", "link"],
            "docker": ["docker", "workdir", "apt-get", "copy", "run", "entrypoint"],
            "swift": ["swift", "package.swift", "spm", "tools-version"],
            "path": ["no such file", "directory", "cannot stat", "path", "mkdir"]
        }

        selected_principles = []
        all_principles = kb.get("general_principles", [])

        # 命中逻辑
        hit_categories = [cat for cat, kws in category_map.items() if any(kw in log_sample for kw in kws)]

        for p in all_principles:
            if any(cat in p.lower() for cat in hit_categories):
                selected_principles.append(p)

        # 配额管理：如果没命中则取前 3 条；如果命中了则取最相关的 6 条
        if not selected_principles:
            final_principles = all_principles[:3]
        else:
            final_principles = selected_principles[:6]

        # 模式匹配建议（保持原有的高效正则逻辑）
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
    【专业修复版】锁定基础镜像 Digest，并移除 git clone 中的 --depth 1 以支持 SHA 切换。
    解决了旧版本正则无法处理带数字/连字符标签（如 :24-04）导致镜像格式损坏的问题。
    """
    print(f"--- Tool: patch_project_dockerfile for {project_name} ---")
    dockerfile_path = os.path.join(oss_fuzz_path, "projects", project_name, "Dockerfile")
    if not os.path.exists(dockerfile_path):
        return {'status': 'skip', 'message': 'Dockerfile not found.'}

    try:
        with open(dockerfile_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 1. 替换基础镜像 Digest
        if base_image_digest:
            # 正则逻辑：
            # (FROM\s+gcr.io/oss-fuzz-base/base-builder[^\s:@]*) -> 捕获镜像名及变体（如 base-builder-python）
            # [^\s]* -> 匹配并消耗掉后面紧跟的所有非空字符（即旧的 :tag 或 @sha256:...）
            pattern = r'(FROM\s+gcr.io/oss-fuzz-base/base-builder[^\s:@]*)'
            replacement = r'\1' + f'@sha256:{base_image_digest}'
            
            # 使用 re.IGNORECASE 增强鲁棒性，并确保替换掉整行镜像声明
            content = re.sub(pattern + r'[^\s]*', replacement, content, flags=re.IGNORECASE)

        # 2. 移除 Dockerfile 里的 --depth 1 或 --depth=1，确保 git checkout 能找到历史 Commit
        # 使用正则处理可能的空格变体
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


def save_commit_diff_to_file(project_name: str, project_source_path: str, sha: str, error_time: str):
    """
    【带 Token 防御版】提取最近变更，并根据长度执行三级精简。
    """
    import os
    import subprocess
    print(f"--- Tool: save_commit_diff_to_file (With Token Guard) for {sha} ---")
    
    TOKEN_GUARD_CHARS = 12000 # 约 3000 tokens
    OUTPUT_PATH = "generated_prompt_file/commit_changed.txt"
    os.makedirs("generated_prompt_file", exist_ok=True)

    try:
        # 获取原始 Diff
        raw_diff_res = subprocess.run(["git", "-C", project_source_path, "show", sha], 
                                      capture_output=True, text=True, check=True)
        content = raw_diff_res.stdout

        # 执行精简提取逻辑
        if len(content) > TOKEN_GUARD_CHARS:
            print(f"  - Content length ({len(content)}) exceeds guard. Simplifying...")
            
            # 一级精简：移除背景行 (只保留 @, +, - 开头的行)
            lines = content.split('\n')
            simplified = [l for l in lines if l.startswith(('+', '-', '@', 'commit', 'Author', 'Date'))]
            content = "\n".join(simplified)
            
            # 二级精简：如果还长，仅保留文件名和变更摘要
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
    【修复版】读取项目信息。
    增加了对 'state' 字段的检查，并兼容处理 YAML 中的布尔值。
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
            # --- 核心修复逻辑：增强状态判定 ---
            # 兼容处理字符串 'no'/'yes' 和 布尔值 False/True
            fixed_state = str(entry.get('fixed_state', 'no')).lower()
            state = str(entry.get('state', 'no')).lower()

            # 只有当 fixed_state 和 state 均为 'no' 时，才认为该项目需要处理
            if fixed_state == 'no' and state == 'no':
                project_name = entry.get('project')
                sha = entry.get('oss-fuzz_sha')
                error_time_str = str(entry.get('error_time', ""))
                fuzzing_build_error_log_url = entry.get('fuzzing_build_error_log', "")

                if project_name and sha:
                    log_dir = os.path.join("build_error_log", project_name)
                    original_log_path = ""

                    # 1. 优先处理远程日志
                    if fuzzing_build_error_log_url.startswith("http"):
                        download_result = download_remote_log(fuzzing_build_error_log_url, project_name, error_time_str)
                        if download_result['status'] == 'success':
                            original_log_path = download_result['local_path']
                    
                    # 2. 远程失败或无URL，则本地查找
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

                    # 3. 构造项目信息
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
    【闭环回显版】应用极致精简补丁，并在失败时返回文件真实内容以供对齐。
    """
    import os, difflib
    print(f"--- Tool: apply_patch (with Feedback) called ---")
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

            if not os.path.exists(file_path):
                errors.append(f"File not found: {file_path}")
                continue
            with open(file_path, 'r', encoding='utf-8') as f:
                file_content = f.read()

            if original_block in file_content:
                new_content = file_content.replace(original_block, replacement_block, 1)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                total_lines_changed += max(len(original_block.splitlines()), len(replacement_block.splitlines()))
                modified_files.add(file_path)
                applied_count += 1
            else:
                # 匹配失败：寻找最相似的区域并回显给 Agent 
                lines = file_content.splitlines()
                # 提取 ORIGINAL 块的第一行作为搜索锚点
                search_anchor = original_block.splitlines()[0].strip()
                matches = difflib.get_close_matches(search_anchor, lines, n=1, cutoff=0.3)
                
                actual_context = "Unknown context (File may be too different)"
                if matches:
                    idx = lines.index(matches[0])
                    # 取匹配行前后 5 行供 Agent 参考原文格式（包括空格和注释）
                    actual_context = "\n".join(lines[max(0, idx-5):min(len(lines), idx+10)])
                
                errors.append(f"MATCH FAILED for {file_path}.\n### ACTUAL CONTENT AROUND TARGET AREA ###\n{actual_context}\n### PLEASE ENSURE ORIGINAL BLOCK MATCHES EXACTLY ###")

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
    【防御性熔断版】支持剥离 License 后查看内容，并对百分比模式设置 500 行硬上限。
    mode: "full", "tail_50", "tail_30", "tail_100_lines"
    """
    import os, re
    print(f"--- Tool: read_file_content (Mode: {mode}) called for: {file_path} ---")
    if not os.path.isfile(file_path):
        return {"status": "error", "message": f"File not found: {file_path}"}
    try:
        with open(file_path, "r", encoding="utf-8", errors='ignore') as f:
            lines = f.readlines()

        # 1. 自动剥离 License 头部
        license_pattern = re.compile(r"^(#|//|\s*\*|/\*).*$", re.MULTILINE)
        start_idx = 0
        for i, line in enumerate(lines[:50]):
            if line.strip() and not license_pattern.match(line):
                start_idx = i
                break
        if start_idx > 5:
            lines = lines[start_idx:]
            print(f"--- Stripped license header ({start_idx} lines) ---")

        total_lines = len(lines)

        # 2. 根据模式进行切片，并引入 500 行硬熔断策略
        if mode == "tail_50":
            target_count = int(total_lines * 0.5)
            # 硬熔断：如果 50% 超过 500 行，强制降级
            if target_count > 500:
                print(
                    f"--- [SAFETY MELT] tail_50 ({target_count} lines) exceeds limit. Falling back to tail_100_lines. ---")
                lines = lines[-100:]
                mode = "tail_100_lines (melted)"
            else:
                lines = lines[-target_count:]
        elif mode == "tail_30":
            target_count = int(total_lines * 0.3)
            # 硬熔断：如果 30% 超过 500 行，强制降级
            if target_count > 500:
                print(
                    f"--- [SAFETY MELT] tail_30 ({target_count} lines) exceeds limit. Falling back to tail_100_lines. ---")
                lines = lines[-100:]
                mode = "tail_100_lines (melted)"
            else:
                lines = lines[-target_count:]
        elif mode == "tail_100_lines":
            lines = lines[-100:]
        elif mode == "full":
            # 即使是 full 模式，也进行一次最后的长度防御（如 1000 行）
            if total_lines > 1000:
                print(f"--- [SAFETY MELT] full mode exceeds 1000 lines. Truncating to tail_500. ---")
                lines = lines[-500:]
                mode = "full (truncated to 500)"

        content = "".join(lines)
        return {
            "status": "success",
            "message": f"Read {len(lines)} lines from {file_path} (Mode: {mode})",
            "content": content
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


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
                         expert_knowledge: str = "", enhanced_history: str = "") -> dict:
    """
    【源码预算调度版 - 全量替换】
    1. 物理预算：GLOBAL_CHAR_BUDGET = 280,000 (约 80k Token)。
    2. 优先级加载：核心报错文件 > 构建脚本 > 其他文件。
    3. 动态降级：当预算接近耗尽时，自动从 full 模式降级为 tail_30 或 name_only。
    """
    import os, re
    from agent_tools import read_file_content, save_file_tree_shallow, truncate_prompt_file

    PROMPT_DIR = "generated_prompt_file"
    PROMPT_FILE_PATH = os.path.join(PROMPT_DIR, "prompt.txt")
    FUZZ_LOG_PATH = "fuzz_build_log_file/fuzz_build_log.txt"

    # --- 80,000 Token 预算对应的字符配额 ---
    GLOBAL_CHAR_BUDGET = 280000
    current_used = 0

    # Step 1: 识别 Level 1 优先级文件（通过报错日志和 HAFix 报告提取）
    context_stream = expert_knowledge + enhanced_history
    if os.path.exists(FUZZ_LOG_PATH):
        with open(FUZZ_LOG_PATH, 'r', encoding='utf-8') as lf:
            context_stream += "".join(lf.readlines()[-50:])

    # 提取潜在的文件名路径
    candidates = re.findall(r"([\w\-\./]+\.(?:c|cpp|h|cc|swift|sh|py|java))", context_stream)
    l1_filenames = set([os.path.basename(c) for c in candidates])

    with open(PROMPT_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(f"Testing Expert. Project: {os.path.basename(project_main_folder_path)}. Attempt: {attempt_id}\n")
        f.write(f"\n【ENHANCED HISTORY】\n{enhanced_history}\n")
        f.write(f"\n【STRATEGIC KNOWLEDGE】\n{expert_knowledge}\n")

        all_configs = sorted(os.listdir(config_folder_path))

        # --- Level 1: 核心关联文件 (Full) ---
        for fname in [cfg for cfg in all_configs if cfg in l1_filenames]:
            res = read_file_content(os.path.join(config_folder_path, fname), mode="full")
            c = res.get('content', '')
            f.write(f"\n### {fname} (Priority High) ###\n{c}\n")
            current_used += len(c)

        # --- Level 2: 核心构建配置 (Dynamic) ---
        for fname in [cfg for cfg in all_configs if
                      cfg not in l1_filenames and (cfg.endswith('.sh') or 'Dockerfile' in cfg)]:
            # 如果配额已消耗超过 60%，降级为 tail_50
            mode = "full" if current_used < (GLOBAL_CHAR_BUDGET * 0.6) else "tail_50"
            res = read_file_content(os.path.join(config_folder_path, fname), mode=mode)
            c = res.get('content', '')
            f.write(f"\n### {fname} (Mode: {mode}) ###\n{c}\n")
            current_used += len(c)

        # --- Level 3: 辅助文件 (Safe Limit) ---
        for fname in [cfg for cfg in all_configs if
                      cfg not in l1_filenames and not cfg.endswith('.sh') and 'Dockerfile' not in cfg]:
            if current_used > GLOBAL_CHAR_BUDGET:
                f.write(f"\n### {fname} ###\n[Content omitted: Context budget full]\n")
            else:
                res = read_file_content(os.path.join(config_folder_path, fname), mode="tail_30")
                c = res.get('content', '')
                f.write(f"\n### {fname} (tail_30) ###\n{c}\n")
                current_used += len(c)

        # 注入文件树与日志末尾
        save_file_tree_shallow(project_main_folder_path, 1, os.path.join(PROMPT_DIR, "file_tree.txt"))
        log_res = read_file_content(FUZZ_LOG_PATH, mode="tail_100_lines")
        f.write(f"\n\n--- BUILD LOG TAIL ---\n{log_res.get('content', '')}")

    truncate_prompt_file(PROMPT_FILE_PATH, max_lines=2500)
    with open(PROMPT_FILE_PATH, "r", encoding="utf-8") as rf:
        return {"status": "success", "content": rf.read()}


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
