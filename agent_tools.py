# file_tools.py
# 这是一个可供多个Agent共享的文件操作工具箱。

import os
from typing import Optional, List

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


# --- 工具 : 读取并展示文件内容 ---
def read_file_content(file_path: str) -> dict:
    """
    读取指定文本文件的内容并返回。

    Args:
        file_path (str): 要读取的文件的路径。

    Returns:
        dict: 包含操作结果的字典。
              - 'status' (str): 'success' 或 'error'。
              - 'content' (str): 如果成功，此键包含文件的完整内容。
              - 'message' (str): 如果失败，此键包含错误信息。
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
        message = f"错误：文件 '{file_path}' 过大，无法在聊天中完整显示。"
        print(message)
        return {"status": "error", "message": message}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        # 直接打印内容，而不是将其返回给LLM
        print("\n--- 文件内容开始 ---")
        print(content)
        print("--- 文件内容结束 ---\n")
        # 只返回一个轻量级的成功消息
        message = f"文件 '{file_path}' 的内容已成功读取并显示在上方。"
        return {"status": "success", "message": message}
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


# --- prompt_generate_agent工作流工具 ---
def prompt_generate_tool(project_main_folder_path: str, config_files: List[str]) -> dict:
    """
    自动化地收集多种fuzzing上下文信息，并将它们整合到一个prompt文件中。

    这个高级工具会按顺序执行以下操作：
    1. 创建一个干净的prompt文件。
    2. 将所有指定的配置文件内容追加进去。
    3. 生成并追加项目的完整文件树。
    4. 如果存在，则追加fuzz构建日志。

    Args:
        project_main_folder_path (str): 需要分析的项目的主文件夹路径。
        config_files (List[str]): 一个包含所有相关fuzz配置文件路径的列表。

    Returns:
        dict: 包含整个工作流执行状态和最终结果信息的字典。
    """
    print("--- Workflow Tool: prompt_generate_tool started ---")

    # 定义标准化的文件路径
    PROMPT_DIR = "generated_prompt_file"
    PROMPT_FILE_PATH = os.path.join(PROMPT_DIR, "prompt.txt")
    FILE_TREE_PATH = os.path.join(PROMPT_DIR, "file_tree.txt")
    FUZZ_LOG_PATH = "fuzz_build_log_file/fuzz_build_log.txt"

    # --- 步骤 1: 创建一个干净的 prompt.txt 文件 ---
    print("Step 1: Creating a new prompt file...")
    result = create_or_update_file(file_path=PROMPT_FILE_PATH, content="--- Fuzzing Context Prompt ---\n\n")
    if result["status"] == "error":
        return result  # 如果第一步失败，则中止整个工作流

    # --- 步骤 2: 追加配置文件内容 ---
    print("Step 2: Appending configuration files...")
    append_string_to_file(PROMPT_FILE_PATH, "\n--- Configuration Files ---\n")
    for config_file in config_files:
        print(f"  - Appending '{config_file}'...")
        result = append_file_to_file(source_path=config_file, destination_path=PROMPT_FILE_PATH)
        if result["status"] == "error":
            print(f"    Warning: Failed to append '{config_file}': {result['message']}. Skipping.")

    # --- 步骤 3: 生成项目文件树 ---
    print("Step 3: Generating project file tree...")
    # 调用 save_file_tree，它会默认保存到 FILE_TREE_PATH
    result = save_file_tree(directory_path=project_main_folder_path)
    if result["status"] == "error":
        return result  # 如果失败则中止

    # --- 步骤 4: 追加文件树到 prompt.txt ---
    print("Step 4: Appending file tree to prompt file...")
    append_string_to_file(PROMPT_FILE_PATH, "\n--- Project File Tree ---\n")
    result = append_file_to_file(source_path=FILE_TREE_PATH, destination_path=PROMPT_FILE_PATH)
    if result["status"] == "error":
        return result

    # --- 步骤 5: (可选) 追加fuzz构建日志 ---
    print("Step 5: Checking for and appending fuzz build log...")
    if os.path.isfile(FUZZ_LOG_PATH) and os.path.getsize(FUZZ_LOG_PATH) > 0:
        print(f"  - Found fuzz log at '{FUZZ_LOG_PATH}'. Appending...")
        append_string_to_file(PROMPT_FILE_PATH, "\n--- Fuzz Build Log ---\n")
        result = append_file_to_file(source_path=FUZZ_LOG_PATH, destination_path=PROMPT_FILE_PATH)
        if result["status"] == "error":
            print(f"    Warning: Failed to append fuzz log: {result['message']}.")
    else:
        print("  - Fuzz log not found or is empty. Skipping.")

    final_message = f"Prompt生成工作流成功完成。所有上下文信息已整合到 '{PROMPT_FILE_PATH}' 文件中。"
    print(f"--- Workflow Tool: prompt_generate_tool finished successfully ---")
    return {"status": "success", "message": final_message}
