#!/bin/bash
rm solution*
# 检查是否传入了项目名称参数
if [ -z "$1" ]; then
    echo "Error: Project name argument is required."
    echo "Usage: ./cleanup.sh <project_name>"
    exit 1
fi

PROJECT_NAME="$1"

echo "--- Cleaning up environment for project: $PROJECT_NAME ---"

# 1. 删除构建日志目录
# Python: shutil.rmtree("fuzz_build_log_file")
if [ -d "fuzz_build_log_file" ]; then
    rm -rf "fuzz_build_log_file"
fi

# 2. 删除生成的 Prompt 和中间文件
# Python: shutil.rmtree("generated_prompt_file")
if [ -d "generated_prompt_file" ]; then
    rm -rf "generated_prompt_file"
fi

# 3. 删除生成的修复方案
# Python: os.remove("solution.txt")
if [ -f "solution.txt" ]; then
    rm -f "solution*"
fi

# 4. 删除第三方软件的源代码
# 逻辑：过滤项目名称，只保留字母、数字、下划线和连字符
# Python: "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
SAFE_PROJECT_NAME=$(echo "$PROJECT_NAME" | sed 's/[^a-zA-Z0-9_-]//g')

# 构建完整路径
# Python: os.path.join(os.getcwd(), "process", "project", safe_project_name)
CURRENT_DIR=$(pwd)
PROJECT_SOURCE_PATH="$CURRENT_DIR/process/project/$SAFE_PROJECT_NAME"

if [ -d "$PROJECT_SOURCE_PATH" ]; then
    rm -rf "$PROJECT_SOURCE_PATH"
    # 检查上一条命令是否执行成功
    if [ $? -eq 0 ]; then
        echo "--- Removed project source at: $PROJECT_SOURCE_PATH ---"
    else
        echo "--- Warning: Failed to remove project source at: $PROJECT_SOURCE_PATH ---"
    fi
fi

echo "--- Cleanup complete. ---"
