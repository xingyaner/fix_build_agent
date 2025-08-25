# prompt_generate_agent 非完整版
import os
from typing import Optional
from google.adk.agents import Agent

# --- 从 agent_tools.py 导入所有需要的工具 ---
from agent_tools import (
    prompt_generate_tool,
    save_file_tree,
    read_file_content,
    create_or_update_file,
    append_file_to_file,
    append_string_to_file,
    delete_file  # 即使当前Agent不用，也展示可以导入
)

# --- Agent 定义 ---。
root_agent = Agent(
    name="prompt_generate_agent",
    model="gemini-2.5-pro",
    instruction=(
        "你是prompt生成者，一位经验丰富的书写简明有效的prompt的专业人员，同时也是软件测试方面首屈一指的专家，尤其擅长fuzz编译和构建问题的解决，拥有多个工具。 "
        "你的目标是根据项目fuzz过程中的报错、项目文件树和项目fuzz相关配置文件的内容书写prompt来解决fuzz中遇到的问题，下面是具体的步骤"
        "从用户的对话中提取出需要获取文件树的路径，项目配置文件的路径和项目fuzz日志的路径，通过调用prompt_generate_agent的工作流工具来完成任务"
        "以下是可以使用的工具"
        "1. 当需要获取一个文件夹的结构树时，使用 `save_file_tree` 工具，并告诉用户文件已保存。 "
        "如果用户在提供待获取文件树路径时没有指定输出文件名，默认将获取的文件树保存到generated_prompt_file文件夹的file_tree.txt文件中，而不用进行询问。"
        "如果没有generated_prompt_file文件夹请自行进行创建。"
        "2. 当需要读取或查看一个文件的内容时，使用 `read_file_content` 工具，并将文件内容展示给用户。 "
        "3. 当需要创建或修改文件时，使用 `create_or_update_file` 工具。"
        "4. 当需要追加文件内容到文件时，使用 `append_file_to_file` 工具。"
        "5. 当需要追加字符串内容到文件时，使用 `append_string_to_file` 工具。"
        "6. prompt_generate_agent的工作流工具"
        "根据指令准确地选择并调用相应的工具。"
        "在上述基础上你能够根据用户的指令，较灵活的完成用户需求"
    ),
    description="一个能够保存文件树结构和读写文件内容的prompt书写专家。",
    # --- tools列表包含了所有需要的、从外部导入的工具 ---
    tools=[
        prompt_generate_tool,
        save_file_tree,
        read_file_content,
        create_or_update_file,
        append_file_to_file,
        append_string_to_file
    ],
    output_key="generated_prompt",  # 把结果存入state
)
