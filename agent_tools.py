# file_tools.py
# 这是一个可供多个Agent共享的文件操作工具箱。

import os
import subprocess 
from typing import Optional, List
from collections import deque

# --- 工具 : 保存文件树 ---
def save_file_tree(directory_path: str, output_file: Optional[str] = None) -> dict:
    """
    获取指定路径下文件夹的文件树结构，并将其保存到文件中。

    Args:
        directory_path (str): 目标文件夹的绝对或相对路径。
        output_file (str, optional): 用于保存文件树的输出文件名。
                                     如果未提供，按照默认文件路径进行保存，默认值将会在agent调用时提供。

    Returns:
        dict: 包含操作结果的字典。
              - 'status' (str): 'success' 或 'error'。
              - 'message' (str): 操作结果的摘要信息。
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
            print(f"--- Ensured directory exists: {output_dir} ---")

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


# --- 工具 : 读取文件内容 ---
def read_file_content(file_path: str) -> dict:
    """
    读取指定文本文件的内容并返回。

    Args:
        file_path (str): 要读取的文件的路径。

    Returns:
        dict: 包含操作结果的字典。
              - 'status' (str): 'success' 或 'error'。
              - 'content' (str): 如果成功，此键包含文件的完整内容。
              - 'message' (str): 操作结果的摘要信息或错误信息。
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
        #  创建一条简洁的成功消息。
        success_message = f"文件 '{file_path}' 的内容已成功读取并加载到内存中。"
        print(success_message) # 只在控制台打印这条成功消息。

        # 将读取到的'content'包含在返回的字典中，供Agent使用。
        return {"status": "success", "message": success_message, "content": content}
        # --- 结束修改 ---

    except Exception as e:
        message = f"读取文件 '{file_path}' 时发生错误: {str(e)}"
        print(message)
        return {"status": "error", "message": message}


# --- 新增工具 : 创建或更新文件 ---
def create_or_update_file(file_path: str, content: str) -> dict:
    """
    创建一个新文件并写入内容，或者覆盖一个已存在的文件。

    Args:
        file_path (str): 要创建或更新的文件的路径。
        content (str): 要写入文件的完整内容。

    Returns:
        dict: 包含操作结果的字典。
    """
    print(f"--- Tool: create_or_update_file called for path: {file_path} ---")
    try:
        # 提取文件所在的目录
        directory = os.path.dirname(file_path)
        # 如果目录不存在，则创建它
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


# --- 新增工具: 追加文件到文件 ---
def append_file_to_file(source_path: str, destination_path: str) -> dict:
    """
    读取一个源文件的全部内容，并将其追加到目标文件的末尾。

    Args:
        source_path (str): 要读取内容的源文件的路径。
        destination_path (str): 要追加内容的目标文件的路径。如果该文件不存在，将会被创建。
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


# --- 新增工具: 追加字符串到文件 ---
def append_string_to_file(file_path: str, content: str) -> dict:
    """
    在指定文件的末尾追加一段字符串内容。

    Args:
        file_path (str): 要追加内容的文件的路径。
        content (str): 要追加到文件末尾的字符串。
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


# --- 新增工具 : 删除文件 ---
def delete_file(file_path: str) -> dict:
    """
    删除一个指定的文件。这是一个危险操作。

    Args:
        file_path (str): 要删除的文件的路径。

    Returns:
        dict: 包含操作结果的字典。
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


# --- 新增工具 prompt_generate 工作流工具 ---
def prompt_generate_tool(project_main_folder_path: str, config_folder_path: str) -> dict:
    """
    自动化地收集多种fuzzing上下文信息，并将它们整合到一个prompt文件中。

    这个高级工具会自动扫描指定的config_folder_path目录，处理其中的所有文件。
    它会按顺序执行以下操作：
    1. 动态生成并写入引导性的开场白。
    2. 将所有发现的配置文件内容追加进去。
    3. 生成并追加项目的完整文件树。
    4. 如果存在，则追加fuzz构建日志。

    Args:
        project_main_folder_path (str): 需要分析的项目的主文件夹路径。
        config_folder_path (str): 包含所有相关fuzz配置文件的目录的路径。

    Returns:
        dict: 包含整个工作流执行状态和最终结果信息的字典。
    """
    print("--- Workflow Tool: prompt_generate_tool started ---")

    # 定义标准化的文件路径
    PROMPT_DIR = "generated_prompt_file"
    PROMPT_FILE_PATH = os.path.join(PROMPT_DIR, "prompt.txt")
    FILE_TREE_PATH = os.path.join(PROMPT_DIR, "file_tree.txt")
    FUZZ_LOG_PATH = "fuzz_build_log_file/fuzz_build_log.txt"

    # --- 自动发现配置文件 ---
    print(f"Step 0: Discovering configuration files in '{config_folder_path}'...")
    if not os.path.isdir(config_folder_path):
        return {"status": "error", "message": f"错误：提供的配置文件路径 '{config_folder_path}' 不是一个有效的目录。"}

    try:
        # 使用os.listdir()获取目录下所有条目，并用os.path.join()构建完整路径
        # 同时过滤掉子目录，只保留文件
        all_config_files = [
            os.path.join(config_folder_path, f)
            for f in sorted(os.listdir(config_folder_path))
            if os.path.isfile(os.path.join(config_folder_path, f))
        ]
        if not all_config_files:
            print(f"Warning: 在目录 '{config_folder_path}' 中没有找到任何文件。")
    except Exception as e:
        return {"status": "error", "message": f"扫描配置文件目录时发生错误: {str(e)}"}

    # --- 动态构建背景信息 ---
    print("Step 1: Generating and writing the introductory prompt...")
    project_name = os.path.basename(os.path.abspath(project_main_folder_path))

    # 从自动发现的文件列表中提取文件名
    config_file_names = [os.path.basename(f) for f in all_config_files]
    config_files_str = "、".join(config_file_names) if config_file_names else "（无）"

    introductory_prompt = f"""
    你是软件测试方面首屈一指的专家，尤其擅长fuzz编译和构建问题的解决。通常是由fuzz配置文件与项目的文件内容不匹配导致的编译或构建问题。下面我将给你提供不同项目在oss-fuzz编译过程中的报错，请你根据报错信息和配置文件内容等信息对报错给出针对性的解决方案，尽可能的不去改动与问题不相关的文件内容，最终使该项目能够成功的进行编译和build。
    下面将给出{project_name}的{config_files_str}、文件树、报错日志内容。请你对文件树进行读取并分析给出的信息并且指出问题可能是由哪些文件内容引起的，是fuzz测试构建的核心文件如Dockerfile、build.sh或者是{project_name}项目中的文件，并尝试给出解决方案。
"""

    result = create_or_update_file(file_path=PROMPT_FILE_PATH, content=introductory_prompt)
    if result["status"] == "error":
        return result

    # --- 遍历自动发现的文件列表 ---
    print("Step 2: Appending configuration files...")
    append_string_to_file(PROMPT_FILE_PATH, "\n\n--- Configuration Files ---\n")
    for config_file in all_config_files:  # <-- 现在遍历的是 all_config_files
        file_name = os.path.basename(config_file)
        append_string_to_file(PROMPT_FILE_PATH, f"\n### 内容来源: {file_name} ###\n")
        print(f"  - Appending '{config_file}'...")
        result = append_file_to_file(source_path=config_file, destination_path=PROMPT_FILE_PATH)
        if result["status"] == "error":
            print(f"    Warning: Failed to append '{config_file}': {result['message']}. Skipping.")

    print("Step 3: Generating project file tree...")
    result = save_file_tree(directory_path=project_main_folder_path, output_file=FILE_TREE_PATH)
    if result["status"] == "error":
        return result

    print("Step 4: Appending file tree to prompt file...")
    append_string_to_file(PROMPT_FILE_PATH, "\n\n--- Project File Tree ---\n")
    result = append_file_to_file(source_path=FILE_TREE_PATH, destination_path=PROMPT_FILE_PATH)
    if result["status"] == "error":
        return result

    print("Step 5: Checking for and appending fuzz build log...")
    if os.path.isfile(FUZZ_LOG_PATH) and os.path.getsize(FUZZ_LOG_PATH) > 0:
        print(f"  - Found fuzz log at '{FUZZ_LOG_PATH}'. Appending...")
        append_string_to_file(PROMPT_FILE_PATH, "\n\n--- Fuzz Build Log ---\n")
        result = append_file_to_file(source_path=FUZZ_LOG_PATH, destination_path=PROMPT_FILE_PATH)
        if result["status"] == "error":
            print(f"    Warning: Failed to append fuzz log: {result['message']}.")
    else:
        print("  - Fuzz log not found or is empty. Skipping.")

    final_message = f"Prompt生成工作流成功完成。所有上下文信息已整合到 '{PROMPT_FILE_PATH}' 文件中。"
    print(f"--- Workflow Tool: prompt_generate_tool finished successfully ---")
    return {"status": "success", "message": final_message}


# --- 新增工具 Fuzzing自动执行 ---
def run_fuzz_build(
        project_name: str,
        oss_fuzz_path: str,
        sanitizer: str = "address",
        engine: str = "libfuzzer",
        architecture: str = "x86_64"
) -> dict:
    """
    在指定的OSS-Fuzz目录中，执行一个预定义的fuzzing构建命令，并捕获其输出和错误信息。

    Args:
        project_name (str): 要进行fuzzing的项目名称 (例如 'suricata')。
        oss_fuzz_path (str): OSS-Fuzz所在的路径，以'/oss-fuzz'为路径的最后一部分。
        sanitizer (str, optional): 使用的消毒器。默认为 'address'。
        engine (str, optional): 使用的fuzzing引擎。默认为 'libfuzzer'。
        architecture (str, optional): 目标架构。默认为 'x86_64'。

    Returns:
        dict: 包含命令执行结果的字典，包括状态、stdout和stderr。
    """
    print(f"--- Tool: run_fuzz_build called for project: {project_name} ---")

    try:
        helper_script_path = os.path.join(oss_fuzz_path, "infra/helper.py")
        # --- 安全措施：将命令构建为列表，而不是单个字符串 ---
        # 这可以防止shell注入攻击, 只允许执行这一个特定的脚本。
        command = [
            "python3", helper_script_path, "build_fuzzers",
            "--sanitizer", sanitizer,
            "--engine", engine,
            "--architecture", architecture,
            project_name
        ]

        print(f"--- Executing command: {' '.join(command)} ---")

        # 执行命令并捕获输出
        # text=True: 将输出解码为字符串
        # capture_output=True: 捕获stdout和stderr
        # check=False: 即使命令失败（返回非零代码），也不抛出异常，以便我们能捕获错误信息
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            cwd=oss_fuzz_path
        )

        # 检查命令是否成功执行
        if result.returncode == 0:
            # 成功
            message = f"Fuzzing构建命令成功完成，项目: {project_name}。"
            print(message)
            return {
                "status": "success",
                "message": message,
                "stdout": result.stdout,
                "stderr": result.stderr
            }
        else:
            # 失败
            message = f"Fuzzing构建命令失败，项目: {project_name}。返回码: {result.returncode}。"
            print(f"--- ERROR: {message} ---")
            return {
                "status": "error",
                "message": message,
                "stdout": result.stdout,
                "stderr": result.stderr  # <--- 关键：返回了详细的报错信息
            }

    except FileNotFoundError:
        message = "错误：命令执行失败，'python3' 或 'infra/helper.py' 未找到。"
        print(f"--- ERROR: {message} ---")
        return {"status": "error", "message": message, "stdout": "", "stderr": ""}
    except Exception as e:
        message = f"执行fuzzing命令时发生未知异常: {str(e)}"
        print(f"--- ERROR: {message} ---")
        return {"status": "error", "message": message, "stdout": "", "stderr": str(e)}


def run_fuzz_build_streaming(
        project_name: str,
        oss_fuzz_path: str,
        sanitizer: str = "address",
        engine: str = "libfuzzer",
        architecture: str = "x86_64"
) -> dict:
    """
    执行一个预定义的fuzzing构建命令，并实时流式传输其输出。
    该工具会直接将结果写入日志文件 'fuzz_build_log_file/fuzz_build_log.txt'。
    如果构建成功，写入文本'success'；如果失败，写入最后的400行日志。
    当输入指令没有指定 sanitizer、engine 和 architecture 的值，那就采取默认值而不必询问

    Args:
        project_name (str): 要进行fuzzing的项目名称。
        oss_fuzz_path (str): OSS-Fuzz项目的根目录的绝对路径，如：/root/oss-fuzz/
        sanitizer (str, optional): 使用的消毒器。
        engine (str, optional): 使用的fuzzing引擎。
        architecture (str, optional): 目标架构。

    Returns:
        dict: 只包含最终状态和摘要信息的字典。日志内容被直接写入文件。
    """
    print(f"--- Tool: run_fuzz_build_streaming called for project: {project_name} ---")

    # --- 核心修改 1: 预先定义日志文件路径 ---
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
            cwd=oss_fuzz_path
        )

        log_buffer = deque(maxlen=400)
        for line in process.stdout:
            print(line, end='', flush=True)
            log_buffer.append(line)

        process.wait()
        return_code = process.returncode

        print("\n--- Fuzzing process finished. ---")

        # --- 核心修改 2: 统一的日志写入逻辑 ---
        # 确保日志目录存在
        os.makedirs(LOG_DIR, exist_ok=True)

        if return_code == 0:
            # 成功时，要写入的内容是 "success"
            content_to_write = "success"
            message = f"Fuzzing构建命令成功完成。结果已保存到 '{LOG_FILE_PATH}'。"
            status = "success"
        else:
            # 失败时，要写入的内容是最后400行日志
            content_to_write = "".join(log_buffer)
            message = f"Fuzzing构建命令失败。详细日志已保存到 '{LOG_FILE_PATH}'。"
            status = "error"

        # 执行文件写入操作
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(content_to_write)

        print(message)  # 在终端打印最终的摘要信息

        # --- 核心修改 3: 返回不包含日志内容的简洁结果 ---
        return {"status": status, "message": message}

    except Exception as e:
        # 在异常情况下，也尝试将错误信息写入日志文件
        message = f"执行fuzzing命令时发生未知异常: {str(e)}"
        print(message)


import os


def apply_solution_file(solution_file_path: str) -> dict:
    """
    解析一个包含文件修改方案的文本文件，并将这些修改应用到指定的目标路径中。
    此工具能够处理包含一个或多个文件修改块的解决方案文件。

    预期的文件格式为：
    ---=== FILE ===---
    指向待修改文件的路径
    文件1替换后的完整内容...
    ---=== FILE ===---
    指向待修改文件的路径
    文件2替换后的完整内容...
    """
    print(
        f"--- Tool: apply_solution_file (最终版) called. Solution File: '{solution_file_path}' ---")

    if not os.path.isfile(solution_file_path):
        return {"status": "error", "message": f"错误：解决方案文件 '{solution_file_path}' 不存在。"}

    try:
        with open(solution_file_path, "r", encoding="utf-8") as f:
            content = f.read()


        print("\n" + "="*20 + " 调试信息: 完整文件内容 " + "="*20)
        print(content)
        print("="*58 + "\n")


        FILE_SEPARATOR = "---=== FILE ===---"

        # 1. 使用分隔符将整个文件内容切分成多个文件块的列表
        file_blocks = content.split(FILE_SEPARATOR)

        parsed_files = {}
        # 2. 循环处理每一个文件块
        for block in file_blocks:
            # 去除每个块前后的空行或空格
            block_content = block.strip()
            if not block_content:
                # 跳过因文件开头就是分隔符而产生的第一个空块
                continue

            # 将处理过的块按行分割
            lines = block_content.split('\n')

            # 【新增调试代码】: 打印处理后的行列表
            print(f"  - 文件块 被分割为以下行:")
            # 循环打印每一行，并显示其索引
            for line_num, line_text in enumerate(lines):
                print(f"    - 行 {line_num}: '{line_text}'")

            # 健壮性检查：确保块至少有一行（路径），内容可以为空
            if len(lines) < 1:
                print(f"--- Warning: Skipping malformed block (empty): {block_content[:80]}... ---")
                continue

            # 3. 正确解析：块的第一行 (lines[0]) 是完整的绝对路径
            full_file_path = lines[0].strip()

            # 4. 正确解析：块的第二行及以后 (lines[1:]) 是文件的完整新内容
            file_content = "\n".join(lines[1:])

            # 简单的路径有效性检查
            if full_file_path and full_file_path.startswith('/'):
                parsed_files[full_file_path] = file_content
            else:
                print(f"--- Warning: Skipping block with invalid or non-absolute path: '{full_file_path}' ---")

        if not parsed_files:
            return {"status": "error",
                    "message": "错误：未能从解决方案文件中解析出任何有效的文件块。请确保格式正确：分隔符后第一行是完整路径。"}

        # 5. 循环写入所有解析出的文件
        updated_files = []
        for target_file_path, content_to_write in parsed_files.items():
            print(f"  - Applying changes to absolute path: '{target_file_path}'...")

            # 确保目标文件的父目录存在
            target_dir_for_file = os.path.dirname(target_file_path)
            if target_dir_for_file:
                os.makedirs(target_dir_for_file, exist_ok=True)

            # 将新内容写入文件，实现整体替换
            with open(target_file_path, "w", encoding="utf-8") as f:
                f.write(content_to_write)

            updated_files.append(target_file_path)

        message = f"解决方案已成功应用。共更新了 {len(updated_files)} 个文件: {', '.join(updated_files)}"
        print(f"--- Tool finished: {message} ---")
        return {"status": "success", "message": message}

    except Exception as e:
        message = f"应用解决方案时发生未知错误: {str(e)}"
        print(f"--- ERROR: {message} ---")
        return {"status": "error", "message": message}
# --- 新增工具: 应用解决方案文件 ---
# def apply_solution_file(solution_file_path: str, target_directory: str) -> dict:
#     """
#     解析一个包含文件修改方案的文本文件，并将这些修改应用到指定的目标目录中。
#     解决方案文件必须使用 '---=== FILE ===---' 作为每个文件块的分隔符。
#     """
#     print(
#         f"--- Tool: apply_solution_file called. Solution: '{solution_file_path}', Target Dir: '{target_directory}' ---")
#
#     if not os.path.isfile(solution_file_path):
#         return {"status": "error", "message": f"错误：解决方案文件 '{solution_file_path}' 不存在。"}
#     if not os.path.isdir(target_directory):
#         return {"status": "error", "message": f"错误：目标目录 '{target_directory}' 不存在。"}
#
#     try:
#         # --- 核心修改：使用分隔符进行解析 ---
#         with open(solution_file_path, "r", encoding="utf-8") as f:
#             content = f.read()
#
#         # 定义分隔符
#         FILE_SEPARATOR = "---=== FILE ===---"
#
#         # 使用分隔符将整个文件内容切分成多个文件块
#         file_blocks = content.split(FILE_SEPARATOR)
#
#         parsed_files = {}
#         for block in file_blocks:
#             if not block.strip():
#                 continue  # 跳过可能存在的空块
#
#             # 将每个块按行分割，并移除前后的空行
#             lines = block.strip().split('\n')
#
#             # 第一行应该是文件名
#             filename = lines[0].strip()
#             # 剩下的所有行都是文件内容
#             file_content = "\n".join(lines[1:])
#
#             if filename:
#                 parsed_files[filename] = file_content
#
#         if not parsed_files:
#             return {"status": "error",
#                     "message": "错误：未能从解决方案文件中解析出任何有效的文件内容。请确保使用了正确的分隔符。"}
#         # --- 结束核心修改 ---
#
#         # --- 文件写入逻辑保持不变 ---
#         updated_files = []
#         for filename, content in parsed_files.items():
#             target_file_path = os.path.join(target_directory, filename)
#             if not os.path.abspath(target_file_path).startswith(os.path.abspath(target_directory)):
#                 print(
#                     f"--- SECURITY WARNING: Skipped writing to '{target_file_path}' as it is outside the target directory. ---")
#                 continue
#             print(f"  - Applying changes to '{target_file_path}'...")
#             target_dir_for_file = os.path.dirname(target_file_path)
#             if target_dir_for_file:
#                 os.makedirs(target_dir_for_file, exist_ok=True)
#             with open(target_file_path, "w", encoding="utf-8") as f:
#                 f.write(content)  # 这里不再需要strip，因为上面的逻辑已经处理好了
#             updated_files.append(filename)
#
#         message = f"解决方案已成功应用。共更新了 {len(updated_files)} 个文件: {', '.join(updated_files)}"
#         print(f"--- Tool finished: {message} ---")
#         return {"status": "success", "message": message}
#
#     except Exception as e:
#         message = f"应用解决方案时发生未知错误: {str(e)}"
#         print(f"--- ERROR: {message} ---")
#         return {"status": "error", "message": message}
