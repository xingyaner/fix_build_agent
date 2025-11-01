import os
import shutil
import subprocess
import json
import openpyxl
from collections import deque
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from google.adk.tools.tool_context import ToolContext

# ==============================================================================
# Section 1: 核心工具
# ==============================================================================
def get_project_paths(project_name: str) -> Dict[str, str]:
    """
    根据项目名称，生成并返回标准的 project_config_path 和 project_source_path。
    """
    print(f"--- Tool: get_project_paths called for: {project_name} ---")
    base_path = os.getcwd()
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


def read_projects_from_excel(file_path: str) -> Dict[str, List[Dict[str, str]]]:
    """
    从指定的 .xlsx 文件中读取项目信息。
    只读取最后一列“报错是否一致”为“是”的行。
    """
    print(f"--- Tool: read_projects_from_excel called for: {file_path} ---")
    if not os.path.exists(file_path):
        return {'status': 'error', 'message': f"Excel file not found at '{file_path}'."}

    projects_to_run = []
    try:
        workbook = openpyxl.load_workbook(file_path)
        sheet = workbook.active
        headers = [cell.value for cell in sheet[1]]

        if "项目名称" not in headers or "日期" not in headers or "报错是否一致" not in headers:
             return {'status': 'error', 'message': "Excel file is missing required columns: '项目名称', '日期', '报错是否一致'."}

        for row in sheet.iter_rows(min_row=2, values_only=True):
            row_data = dict(zip(headers, row))
            if row_data.get("报错是否一致") == "是":
                project_info = {
                    "project_name": row_data["项目名称"],
                    "date": row_data["日期"].strftime('%Y.%m.%d') if isinstance(row_data["日期"], datetime) else str(row_data["日期"])
                }
                projects_to_run.append(project_info)

        return {'status': 'success', 'projects': projects_to_run}
    except Exception as e:
        return {'status': 'error', 'message': f"Failed to read or parse Excel file: {e}"}

def run_command(command: str) -> Dict[str, str]:
    """
    执行一个 shell 命令并返回其输出。这是一个危险的工具，请谨慎使用。
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
    读取一个文件，如果行数超过 max_lines，则从中间截断它，并保留文件头和文件尾。
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
    将成功修复的项目的配置文件目录归档到一个 'success-fix-project' 目录中。
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


def download_github_repo(project_name: str) -> Dict[str, str]:
    """
    在GitHub上搜索项目并克隆。
    """
    print(f"--- Tool: download_github_repo called for: {project_name} ---")
    base_path = os.getcwd()

    if project_name == "oss-fuzz":
        target_dir = os.path.join(base_path, "oss-fuzz")
    else:
        safe_project_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        target_dir = os.path.join(base_path, "process", "project", safe_project_name)

    if os.path.isdir(target_dir):
        print(f"--- Directory '{target_dir}' already exists. Skipping download. ---")
        return {'status': 'success', 'path': target_dir}

    os.makedirs(os.path.dirname(target_dir), exist_ok=True)

    try:
        search_command = ["gh", "search", "repos", project_name, "--sort", "stars", "--order", "desc", "--limit", "1", "--json", "fullName"]
        result = subprocess.run(search_command, capture_output=True, text=True, check=True, encoding='utf-8')
        
        # 【核心修复】处理 gh 命令可能返回列表的情况
        parsed_output = json.loads(result.stdout.strip())
        if isinstance(parsed_output, list) and parsed_output:
            repo_full_name = parsed_output[0]['fullName']
        elif isinstance(parsed_output, dict):
            repo_full_name = parsed_output['fullName']
        else:
            raise ValueError("gh search command returned unexpected empty or invalid JSON.")
            
        repo_url = f"https://github.com/{repo_full_name}.git"
    except Exception as e:
        message = f"ERROR: 'gh' CLI search or JSON parsing failed. Details: {e}"
        return {'status': 'error', 'message': message}

    clone_command = ["git", "clone", repo_url, target_dir]
    if project_name != "oss-fuzz":
        clone_command.insert(2, "--depth=1")

    try:
        subprocess.run(clone_command, check=True, capture_output=True, text=True)
        message = f"Successfully cloned '{project_name}' to '{target_dir}'."
        return {'status': 'success', 'path': target_dir, 'message': message}
    except subprocess.CalledProcessError as e:
        message = f"Git clone failed for '{project_name}': {e.stderr}"
        return {'status': 'error', 'message': message}


# ==============================================================================
# Section 2: 版本回退工具
# ==============================================================================

def find_sha_for_timestamp(commits_file_path: str, error_date: str) -> Dict[str, str]:
    """
    在 commits 文件中为给定日期找到最合适的 commit SHA。
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

def checkout_oss_fuzz_commit(oss_fuzz_path: str, sha: str) -> Dict[str, str]:
    """
    在指定的 oss-fuzz 目录下，执行 git checkout 命令。
    """
    print(f"--- Tool: checkout_oss_fuzz_commit called for SHA: {sha} ---")
    if not os.path.isdir(os.path.join(oss_fuzz_path, ".git")):
        return {'status': 'error', 'message': f"The directory '{oss_fuzz_path}' is not a git repository."}
    
    original_path = os.getcwd()
    try:
        os.chdir(oss_fuzz_path)
        subprocess.run(["git", "switch", "master"], capture_output=True, text=True)
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


# ==============================================================================
# Section 3: 文件操作与Fuzzing工具 (来自您的原始文件)
# ==============================================================================

def apply_patch(solution_file_path: str) -> dict:
    """
    读取一个特殊格式的解决方案文件，并应用其中的代码替换方案。
    """
    print(f"--- Tool: apply_patch (New Version) called for solution file: {solution_file_path} ---")
    try:
        with open(solution_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        file_part = content.split('---=== FILE ===---')[1].strip()
        original_part = file_part.split('---=== ORIGINAL ===---')[1].strip()
        replacement_part = original_part.split('---=== REPLACEMENT ===---')[1].strip()
        file_path = file_part.split('---=== ORIGINAL ===---')[0].strip()
        original_block = original_part.split('---=== REPLACEMENT ===---')[0].strip()
        replacement_block = replacement_part
        if not file_path or not original_block:
            return {"status": "error", "message": "Solution file format is incorrect. Could not parse FILE path or ORIGINAL block."}
        if not os.path.exists(file_path):
            return {"status": "error", "message": f"Target file does not exist: {file_path}"}
        with open(file_path, 'r', encoding='utf-8') as f:
            original_content = f.read()
        if original_block not in original_content:
            return {"status": "error", "message": "The ORIGINAL code block was not found in the target file. The file may have already been modified or the block is incorrect."}
        new_content = original_content.replace(original_block, replacement_block, 1)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        success_message = f"Successfully applied code fix to '{file_path}'."
        print(success_message)
        return {"status": "success", "message": success_message}
    except IndexError:
        error_message = "Failed to parse solution file. Make sure it contains FILE, ORIGINAL, and REPLACEMENT separators."
        print(error_message)
        return {"status": "error", "message": error_message}
    except Exception as e:
        error_message = f"An error occurred while applying the code fix: {str(e)}"
        print(error_message)
        return {"status": "error", "message": error_message}

def save_file_tree(directory_path: str, output_file: Optional[str] = None) -> dict:
    """
    获取指定路径下文件夹的文件树结构，并将其保存到文件中。
    """
    print(f"--- Tool: save_file_tree called for path: {directory_path} ---")
    if not os.path.isdir(directory_path):
        error_message = f"错误：提供的路径 '{directory_path}' 不是一个有效的目录。"
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
        success_message = f"文件树已成功生成并保存到文件 '{final_output_path}' 中。"
        print(success_message)
        return {"status": "success", "message": success_message}
    except Exception as e:
        error_message = f"生成或保存文件树时发生错误: {str(e)}"
        print(error_message)
        return {"status": "error", "message": error_message}

def save_file_tree_shallow(directory_path: str, max_depth: int, output_file: Optional[str] = None) -> dict:
    """
    获取指定路径下文件夹的前n层文件树结构，并将其覆盖写入到文件中。
    """
    print(f"--- Tool: save_file_tree_shallow called for path: {directory_path} with max_depth: {max_depth} ---")
    if not os.path.isdir(directory_path):
        error_message = f"错误：提供的路径 '{directory_path}' 不是一个有效的目录。"
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
        success_message = f"文件树的前 {max_depth} 层已成功生成并保存到 '{final_output_path}'。"
        print(success_message)
        return {"status": "success", "message": success_message}
    except Exception as e:
        error_message = f"生成或保存浅层文件树时发生错误: {str(e)}"
        print(error_message)
        return {"status": "error", "message": error_message}

def find_and_append_file_details(directory_path: str, search_keyword: str, output_file: Optional[str] = None) -> dict:
    """
    根据文件名或部分路径信息查找文件或目录，并将其详细结构追加写入到文件中。
    """
    print(f"--- Tool: find_and_append_file_details called for path: {directory_path} with keyword: '{search_keyword}' ---")
    if not os.path.isdir(directory_path):
        error_message = f"错误：提供的路径 '{directory_path}' 不是一个有效的目录。"
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
            message = f"在 '{directory_path}' 中未找到与 '{search_keyword}' 匹配的文件或目录。"
            print(message)
            with open(final_output_path, "a", encoding="utf-8") as f:
                f.write(f"\n\n--- 对 '{search_keyword}' 的详细查询结果 ---\n")
                f.write(message)
            return {"status": "success", "message": message}
        details_to_append = [f"\n\n--- 对 '{search_keyword}' 的详细查询结果 ---"]
        for path in found_paths:
            relative_path = os.path.relpath(path, directory_path)
            details_to_append.append(f"\n# 匹配路径: {relative_path}")
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
        success_message = f"已将 '{search_keyword}' 的详细搜索结果追加到 '{final_output_path}'。"
        print(success_message)
        return {"status": "success", "message": success_message}
    except Exception as e:
        error_message = f"查找和追加文件详细信息时发生错误: {str(e)}"
        print(error_message)
        return {"status": "error", "message": error_message}

def read_file_content(file_path: str) -> dict:
    """
    读取指定文本文件的内容并返回。
    """
    print(f"--- Tool: read_file_content called for path: {file_path} ---")
    MAX_FILE_SIZE = 1024 * 1024
    if not os.path.exists(file_path):
        message = f"错误：文件 '{file_path}' 不存在。"
        print(message)
        return {"status": "error", "message": message}
    if not os.path.isfile(file_path):
        message = f"错误：路径 '{file_path}' 是一个目录，而不是一个文件。"
        print(message)
        return {"status": "error", "message": message}
    if os.path.getsize(file_path) > MAX_FILE_SIZE:
        message = f"错误：文件 '{file_path}' 过大，无法处理。"
        print(message)
        return {"status": "error", "message": message}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        success_message = f"文件 '{file_path}' 的内容已成功读取并加载到内存中。"
        print(success_message)
        return {"status": "success", "message": success_message, "content": content}
    except Exception as e:
        message = f"读取文件 '{file_path}' 时发生错误: {str(e)}"
        print(message)
        return {"status": "error", "message": message}

def create_or_update_file(file_path: str, content: str) -> dict:
    """
    创建一个新文件并写入内容，或者覆盖一个已存在的文件。
    """
    print(f"--- Tool: create_or_update_file called for path: {file_path} ---")
    try:
        directory = os.path.dirname(file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        message = f"文件 '{file_path}' 已成功创建/更新。"
        print(message)
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"创建或更新文件 '{file_path}' 时发生错误: {str(e)}"
        print(message)
        return {"status": "error", "message": message}

def append_file_to_file(source_path: str, destination_path: str) -> dict:
    """
    读取一个源文件的全部内容，并将其追加到目标文件的末尾。
    """
    print(f"--- Tool: append_file_to_file called. Source: '{source_path}', Destination: '{destination_path}' ---")
    if not os.path.isfile(source_path):
        return {"status": "error", "message": f"错误：源文件 '{source_path}' 不存在或不是一个有效的文件。"}
    if os.path.isdir(destination_path):
        return {"status": "error", "message": f"错误：目标路径 '{destination_path}' 是一个目录，不能作为追加目标。"}
    if os.path.abspath(source_path) == os.path.abspath(destination_path):
        return {"status": "error", "message": "错误：源文件和目标文件不能是同一个文件。"}
    try:
        with open(source_path, "r", encoding="utf-8") as f_source:
            content_to_append = f_source.read()
        dest_directory = os.path.dirname(destination_path)
        if dest_directory:
            os.makedirs(dest_directory, exist_ok=True)
        with open(destination_path, "a", encoding="utf-8") as f_dest:
            f_dest.write(content_to_append)
        return {"status": "success", "message": f"已成功将 '{source_path}' 的内容追加到 '{destination_path}'。"}
    except Exception as e:
        return {"status": "error", "message": f"在追加文件时发生未知错误: {str(e)}"}

def append_string_to_file(file_path: str, content: str) -> dict:
    """
    在指定文件的末尾追加一段字符串内容。
    """
    print(f"--- Tool: append_string_to_file called for path: {file_path} ---")
    try:
        directory = os.path.dirname(file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(content)
        return {"status": "success", "message": f"内容已成功追加到文件 '{file_path}'。"}
    except Exception as e:
        return {"status": "error", "message": f"向文件 '{file_path}' 追加内容时发生错误: {str(e)}"}

def delete_file(file_path: str) -> dict:
    """
    删除一个指定的文件。
    """
    print(f"--- Tool: delete_file called for path: {file_path} ---")
    if not os.path.exists(file_path):
        message = f"错误：文件 '{file_path}' 不存在，无法删除。"
        print(message)
        return {"status": "error", "message": message}
    try:
        os.remove(file_path)
        message = f"文件 '{file_path}' 已被成功删除。"
        print(message)
        return {"status": "success", "message": message}
    except Exception as e:
        message = f"删除文件 '{file_path}' 时发生错误: {str(e)}"
        print(message)
        return {"status": "error", "message": message}

def prompt_generate_tool(project_main_folder_path: str, max_depth: int, config_folder_path: str) -> dict:
    """
    自动化地收集多种fuzzing上下文信息，并将它们整合到一个prompt文件中。
    """
    print("--- Workflow Tool: prompt_generate_tool started ---")
    PROMPT_DIR = "generated_prompt_file"
    PROMPT_FILE_PATH = os.path.join(PROMPT_DIR, "prompt.txt")
    FILE_TREE_PATH = os.path.join(PROMPT_DIR, "file_tree.txt")
    FUZZ_LOG_PATH = "fuzz_build_log_file/fuzz_build_log.txt"
    print(f"Step 0: Discovering configuration files in '{config_folder_path}'...")
    if not os.path.isdir(config_folder_path):
        return {"status": "error", "message": f"错误：提供的配置文件路径 '{config_folder_path}' 不是一个有效的目录。"}
    try:
        all_config_files = [
            os.path.join(config_folder_path, f)
            for f in sorted(os.listdir(config_folder_path))
            if os.path.isfile(os.path.join(config_folder_path, f))
        ]
        if not all_config_files:
            print(f"Warning: 在目录 '{config_folder_path}' 中没有找到任何文件。")
    except Exception as e:
        return {"status": "error", "message": f"扫描配置文件目录时发生错误: {str(e)}"}
    print("Step 1: Generating and writing the introductory prompt...")
    project_name = os.path.basename(os.path.abspath(project_main_folder_path))
    config_file_names = [os.path.basename(f) for f in all_config_files]
    config_files_str = "、".join(config_file_names) if config_file_names else "（无）"
    introductory_prompt = f"""
你是软件测试方面首屈一指的专家，尤其擅长fuzz编译和构建问题的解决。通常是由fuzz配置文件与项目的文件内容不匹配导致的编译或构建问题。下面我将给你提供不同项目在oss-fuzz编译过程中的报错，请你根据报错信息和配置文件内容等信息对报错给出针对 性的解决方案，尽可能的不去改动与问题不相关的文件内容，最终使该项目能够成功的进行编译和build。
下面将给出{project_name}的{config_files_str}、文件树、报错日志内容。请你对文件树进行读取并分析给出的信息并且指出问题可能是由哪些文件内容引起的，是fuzz测试构建的核心文件如Dockerfile、build.sh或者是{project_name}项目中的文件，并尝试给 出解决方案。
"""
    os.makedirs(PROMPT_DIR, exist_ok=True)
    with open(PROMPT_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(introductory_prompt)
    print("Step 2: Appending configuration files...")
    with open(PROMPT_FILE_PATH, "a", encoding="utf-8") as f:
        f.write("\n\n--- Configuration Files ---\n")
    for config_file in all_config_files:
        with open(PROMPT_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n### 内容来源: {os.path.basename(config_file)} ###\n")
        print(f"  - Appending '{config_file}'...")
        try:
            with open(config_file, "r", encoding="utf-8") as source_f, open(PROMPT_FILE_PATH, "a", encoding="utf-8") as dest_f:
                dest_f.write(source_f.read())
        except Exception as e:
            print(f"    Warning: Failed to append '{config_file}': {e}. Skipping.")
    print(f"Step 3: Generating shallow project file tree (max_depth='{max_depth}')...")
    result = save_file_tree_shallow(
        directory_path=project_main_folder_path,
        max_depth=max_depth,
        output_file=FILE_TREE_PATH
    )
    if result["status"] == "error":
        return result
    print("Step 4: Appending file tree to prompt file...")
    with open(PROMPT_FILE_PATH, "a", encoding="utf-8") as f:
        f.write("\n\n--- Project File Tree (Shallow View) ---\n")
    try:
        with open(FILE_TREE_PATH, "r", encoding="utf-8") as source_f, open(PROMPT_FILE_PATH, "a", encoding="utf-8") as dest_f:
            dest_f.write(source_f.read())
    except Exception as e:
        return {"status": "error", "message": f"Failed to append file tree: {e}"}
    print("Step 5: Checking for and appending fuzz build log...")
    if os.path.isfile(FUZZ_LOG_PATH) and os.path.getsize(FUZZ_LOG_PATH) > 0:
        print(f"  - Found fuzz log at '{FUZZ_LOG_PATH}'. Appending...")
        with open(PROMPT_FILE_PATH, "a", encoding="utf-8") as f:
            f.write("\n\n--- Fuzz Build Log ---\n")
        try:
            with open(FUZZ_LOG_PATH, "r", encoding="utf-8") as source_f, open(PROMPT_FILE_PATH, "a", encoding="utf-8") as dest_f:
                dest_f.write(source_f.read())
        except Exception as e:
            print(f"    Warning: Failed to append fuzz log: {e}.")
    else:
        print("  - Fuzz log not found or is empty. Skipping.")
    final_message = (
        f"Prompt生成工作流成功完成。初始上下文信息已整合到 '{PROMPT_FILE_PATH}' 文件中。"
        f"其中包含了项目前'{max_depth}'层的文件结构。请分析现有信息，如果需要深入了解特定目录，"
        f"请使用 'find_and_append_file_details' 工具进行精确查找。"
    )
    print(f"--- Workflow Tool: prompt_generate_tool finished successfully ---")
    return {"status": "success", "message": final_message}

def run_fuzz_build_streaming(
    project_name: str,
    oss_fuzz_path: str,
    sanitizer: str,
    engine: str,
    architecture: str
) -> dict:
    """
    执行一个预定义的fuzzing构建命令，并实时流式传输其输出。
    """
    print(f"--- Tool: run_fuzz_build_streaming called for project: {project_name} ---")
    LOG_DIR = "fuzz_build_log_file"
    LOG_FILE_PATH = os.path.join(LOG_DIR, "fuzz_build_log.txt")
    try:
        helper_script_path = os.path.join(oss_fuzz_path, "infra/helper.py")
        command = [
            "python3", helper_script_path, "build_fuzzers",
            "--sanitizer", sanitizer,
            "--engine", engine,
            "--architecture", architecture,
            project_name
        ]
        print(f"--- Executing command: {' '.join(command)} ---")
        print("--- Fuzzing process started. Real-time output will be displayed below: ---")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=oss_fuzz_path,
            encoding='utf-8'
        )
        log_buffer = deque(maxlen=280)
        for line in process.stdout:
            print(line, end='', flush=True)
            log_buffer.append(line)
        process.wait()
        return_code = process.returncode
        print("\n--- Fuzzing process finished. ---")
        os.makedirs(LOG_DIR, exist_ok=True)
        if return_code == 0:
            content_to_write = "success"
            message = f"Fuzzing构建命令成功完成。结果已保存到 '{LOG_FILE_PATH}'。"
            status = "success"
        else:
            content_to_write = "".join(log_buffer)
            message = f"Fuzzing构建命令失败。详细日志已保存到 '{LOG_FILE_PATH}'。"
            status = "error"
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(content_to_write)
        print(message)
        return {"status": status, "message": message}
    except Exception as e:
        message = f"执行fuzzing命令时发生未知异常: {str(e)}"
        print(message)
        # 异常时也尝试写入日志
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(message)
        return {"status": "error", "message": message}
