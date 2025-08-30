# prompt_generate_agent 非完整版
import os
from typing import Optional
from google.adk.agents import Agent
from .util import load_instruction_from_file

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
    instruction=load_instruction_from_file("prompt_generate_instruction.txt"),
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
